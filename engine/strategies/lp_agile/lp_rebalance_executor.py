"""engine/strategies/lp_agile/lp_rebalance_executor.py — idempotent rebalance state machine.

A rebalance is multi-step (close → re-centre → re-mint). If it fails halfway,
capital can sit as loose tokens outside any pool. This state machine makes the
sequence SAFE and RESUMABLE:

  PLANNED → CLOSING → CLOSED → REMINTING → DONE
                 │                  │
                 ▼                  ▼
           CLOSE_FAILED       REMINT_FAILED  (→ recover_needed: funds are loose
           (capital still      tokens in the wallet, NOT lost — alert + hold,
            in original LP,     never blind-retry)
            nothing lost)

Guarantees:
  • Default DRY-RUN. Real signing only when the guardrails gate returns 'execute'
    (auto-exec on + chain cleared + gas reserve OK) AND signers are injected.
  • Idempotent: every step is journaled to engine/_state/lp_rebalance_jobs.json,
    keyed by plan_id. A crash/resume re-reads the journal and continues from the
    last good state — it NEVER re-closes an already-closed position.
  • Fail-safe ordering: we only re-mint AFTER a confirmed close. A failed close
    leaves the original position intact. A failed re-mint leaves funds in the
    wallet and raises recover_needed (operator-surfaced) rather than looping.

close_fn(plan) -> {"ok": bool, "error": str|None}
mint_fn(plan)  -> {"ok": bool, "error": str|None, "nft_token_id": int|None}
Both injected, so the whole machine is unit-testable without web3.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import lp_guardrails as guard

logger = logging.getLogger("engine.strategies.lp_agile.lp_rebalance_executor")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_JOURNAL = _REPO_ROOT / "engine" / "_state" / "lp_rebalance_jobs.json"

_TERMINAL = {"DONE", "CLOSE_FAILED"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_jobs() -> dict:
    try:
        return json.loads(_JOURNAL.read_text())
    except Exception:
        return {}


def _save_jobs(jobs: dict) -> None:
    try:
        _JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        tmp = _JOURNAL.with_suffix(".tmp")
        tmp.write_text(json.dumps(jobs, indent=2, default=str))
        tmp.replace(_JOURNAL)
    except Exception as exc:                                          # noqa: BLE001
        logger.warning("[rebal-exec] journal write failed: %s", exc)


def execute_rebalance(plan: dict, *, close_fn: Optional[Callable] = None,
                      mint_fn: Optional[Callable] = None,
                      dry_run: bool = True, write_journal: bool = True,
                      _jobs: Optional[dict] = None) -> dict:
    """Run/resume one rebalance to completion-or-safe-stop. Returns the job record.
    `_jobs` is injectable for tests (bypasses disk)."""
    plan_id = plan.get("plan_id") or f"rebal:{plan.get('nft_token_id')}"
    chain = (plan.get("chain") or (plan.get("current_state") or {}).get("chain") or "base").lower()
    proto = (plan.get("protocol") or "").lower()
    notional = float((plan.get("current_state") or {}).get("value_usd") or plan.get("value_usd") or 0)

    jobs = _jobs if _jobs is not None else _load_jobs()
    job = jobs.get(plan_id) or {
        "plan_id": plan_id, "chain": chain, "protocol": proto,
        "nft_token_id": plan.get("nft_token_id"), "notional_usd": notional,
        "state": "PLANNED", "created_iso": _now(), "history": [],
        "recover_needed": False, "tx": {},
    }
    jobs[plan_id] = job

    def _advance(state, **extra):
        job["state"] = state
        job["updated_iso"] = _now()
        job.setdefault("history", []).append({"state": state, "iso": _now(), **extra})
        if write_journal and _jobs is None:
            _save_jobs(jobs)

    # already finished?
    if job["state"] in _TERMINAL:
        return job

    # gate: dry-run unless guardrails clear AND signers wired
    gate = guard.gate(protocol=proto, chain=chain, notional_usd=notional,
                      slippage_pct=None, action="rebalance")
    can_execute = (not dry_run) and gate["mode"] == "execute" and close_fn and mint_fn
    if not can_execute:
        job["mode"] = gate["mode"]
        job["gate_reason"] = gate["reason"] if gate["mode"] != "execute" else (
            "signers not wired" if not (close_fn and mint_fn) else "dry_run flag set")
        _advance("PLANNED", note="dry-run / not cleared")
        return job

    # ── CLOSE ──────────────────────────────────────────────────────────────
    if job["state"] in ("PLANNED", "CLOSING", "CLOSE_FAILED"):
        _advance("CLOSING")
        try:
            res = close_fn(plan)
        except Exception as exc:                                      # noqa: BLE001
            res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not res.get("ok"):
            # capital is STILL in the original LP — safe, nothing lost.
            _advance("CLOSE_FAILED", error=res.get("error"))
            logger.warning("[rebal-exec] %s close failed (position intact): %s",
                           plan_id, res.get("error"))
            return job
        job["tx"]["close"] = res.get("tx_hashes") or res.get("tx") or True
        _advance("CLOSED")
        guard.record_action(notional)

    # ── RE-MINT (new range) ──────────────────────────────────────────────────
    if job["state"] in ("CLOSED", "REMINTING", "REMINT_FAILED"):
        _advance("REMINTING")
        try:
            res = mint_fn(plan)
        except Exception as exc:                                      # noqa: BLE001
            res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not res.get("ok"):
            # funds are loose tokens in the wallet — SAFE but out of LP. Flag for
            # operator recovery; do NOT blind-retry (could double-spend gas / churn).
            job["recover_needed"] = True
            _advance("REMINT_FAILED", error=res.get("error"))
            logger.error("[rebal-exec] %s RE-MINT FAILED — capital is loose tokens in "
                         "wallet, recover_needed=True: %s", plan_id, res.get("error"))
            return job
        job["tx"]["remint"] = res.get("mint_tx_hash") or True
        job["new_nft_token_id"] = res.get("nft_token_id")
        _advance("DONE")
        guard.record_action(notional)
    return job


def recover_pending() -> list:
    """Jobs stuck in REMINT_FAILED (capital loose in wallet) — for operator alerts."""
    return [j for j in _load_jobs().values() if j.get("recover_needed")]
