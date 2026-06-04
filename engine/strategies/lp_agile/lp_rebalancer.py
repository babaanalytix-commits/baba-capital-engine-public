"""engine/strategies/lp_agile/lp_rebalancer.py — LP-REVAMP P3 auto-rebalance loop.

Detection + planning live in rebalance_plan.py; the hard safety rails in
lp_guardrails.py. THIS module is the loop that ties them together. For every
position the planner flags (out-of-range = CRITICAL, near-edge = SOFT), it asks
the guardrails whether it may act, then either:

  • DRY-RUN  — the default. Logs the plan and emits a PWA notification that states
               the EVENT, the ACTION to take, and its COST, for one-tap operator
               approval. Signs nothing.
  • EXECUTE  — only when LP_AUTO_EXECUTE is on AND the chain is cleared AND every
               rail passes. Runs the close→re-centre→re-mint through the executor
               callable the operator injects, then records the action against the
               daily ledger.
  • BLOCKED  — a rail failed (per-tx cap, daily cap, slippage, protocol). Logged +
               surfaced, never silently dropped.

Design note (honesty): the live executor today signs MINTS only — the close side
(decreaseLiquidity+collect+burn) is not yet a single verified signer. So an
EXECUTE decision with no `executor_fn` wired resolves to BLOCKED("executor not
wired"), NOT a silent no-op. Nothing in this file signs; signing happens only
inside the injected callable once that close-side signer is verified.

Output: ops/pwa/serve/lp_rebalance_board.json  (operator/subscriber PWA reads it).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import lp_guardrails as guard
from . import rebalance_plan

logger = logging.getLogger("engine.strategies.lp_agile.lp_rebalancer")

_REPO_ROOT = Path(__file__).resolve().parents[3]
BOARD_OUT = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_rebalance_board.json"

# protocol → chain, so we can gate per-chain even if the snapshot omits `chain`
_PROTO_CHAIN = {
    "slipstream": "base", "aerodrome": "base", "aerodrome-slipstream": "base",
    "uniswap-v3": "base", "prjx": "hyperevm", "project-x": "hyperevm",
    "hyperswap": "hyperevm",
}

_SEVERITY = {"CRITICAL": "high", "SOFT": "medium", "OPPORTUNITY": "low"}


def _infer_chain(plan: dict) -> str:
    cs = plan.get("current_state") or {}
    if cs.get("chain"):
        return str(cs["chain"]).lower()
    if plan.get("chain"):
        return str(plan["chain"]).lower()
    return _PROTO_CHAIN.get((plan.get("protocol") or "").lower(), "base")


def _notification(plan: dict, decision: dict) -> dict:
    """A prompt, unambiguous operator alert: EVENT, ACTION, COST."""
    trig = plan.get("trigger", "?")
    pair = plan.get("pair", "?")
    cs = plan.get("current_state") or {}
    cost = plan.get("est_net_cost_usd")
    gas = plan.get("est_gas_usd")
    mode = decision["mode"]

    if trig == "CRITICAL":
        event = f"{pair} is OUT OF RANGE — earning $0 in fees right now."
    elif trig == "SOFT":
        event = f"{pair} price is near the edge of its range — break-out risk."
    else:
        event = f"{pair}: {plan.get('rationale', 'rebalance opportunity')}"

    n_tx = len(plan.get("tx_sequence") or [])
    if mode == "execute":
        action = f"Auto-rebalancing now ({n_tx} txs): close → re-centre → re-mint."
    elif mode == "dry_run":
        action = (f"Tap to rebalance ({n_tx} txs): close → re-centre on live price "
                  f"→ re-mint" + (" → re-stake." if cs.get("staked") else "."))
    else:  # blocked
        action = f"Rebalance held back: {decision['reason']}."

    cost_str = (f"~${gas:.2f} gas"
                + (f", net ${cost:.2f} after AERO rewards" if cost is not None else ""))
    return {
        "pair": pair,
        "trigger": trig,
        "severity": _SEVERITY.get(trig, "low"),
        "mode": mode,
        "event": event,
        "action": action,
        "cost": cost_str,
        "value_usd": cs.get("value_usd"),
        "nft_token_id": plan.get("nft_token_id"),
        "plan_id": plan.get("plan_id"),
    }


def plan_rebalances(*, snapshot_plans: Optional[dict] = None) -> list[dict]:
    """Build the gated rebalance items (no execution). Each item =
    {plan, decision, notification}. Pure given snapshot_plans → unit-testable."""
    src = snapshot_plans if snapshot_plans is not None else rebalance_plan.run_once(write=False)
    items: list[dict] = []
    for plan in (src.get("plans") or []):
        chain = _infer_chain(plan)
        proto = (plan.get("protocol") or "").lower()
        notional = float((plan.get("current_state") or {}).get("value_usd") or 0.0)
        decision = guard.gate(protocol=proto, chain=chain, notional_usd=notional,
                              slippage_pct=None, action="rebalance")
        items.append({
            "plan": plan,
            "chain": chain,
            "decision": decision,
            "notification": _notification(plan, decision),
        })
    return items


def run_rebalance_cycle(*, executor_fn: Optional[Callable[[dict], dict]] = None,
                        write: bool = True) -> dict:
    """One full pass: detect → gate → (dry-run notify | execute | block) → publish.

    executor_fn(plan) -> {"ok": bool, "error": str|None} is the ONLY thing that may
    sign. When None, EXECUTE decisions resolve to BLOCKED (no silent no-op).
    """
    items = plan_rebalances()
    executed, dry_run, blocked = [], [], []

    for it in items:
        plan, decision = it["plan"], it["decision"]
        notional = float((plan.get("current_state") or {}).get("value_usd") or 0.0)

        if decision["mode"] == "execute":
            if executor_fn is None:
                # Honest: cleared by rails, but no verified close-side signer wired.
                decision = {"mode": "blocked", "ok": False,
                            "reason": "executor not wired (close-side signer pending verification)"}
                it["decision"] = decision
                it["notification"] = _notification(plan, decision)
                blocked.append(it)
                continue
            try:
                res = executor_fn(plan)
            except Exception as exc:                                  # noqa: BLE001
                res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if res.get("ok"):
                guard.record_action(notional)
                it["result"] = res
                executed.append(it)
            else:
                it["result"] = res
                it["decision"] = {"mode": "blocked", "ok": False,
                                  "reason": f"executor failed: {res.get('error')}"}
                it["notification"] = _notification(plan, it["decision"])
                blocked.append(it)
        elif decision["mode"] == "dry_run":
            dry_run.append(it)
        else:
            blocked.append(it)

    board = {
        "generated_at_iso": datetime.now(timezone.utc).isoformat(),
        "auto_execute_enabled": guard.is_auto_execute_enabled(),
        "n_total": len(items),
        "n_executed": len(executed),
        "n_dry_run": len(dry_run),
        "n_blocked": len(blocked),
        "notifications": [it["notification"] for it in items],
        "executed": [it["notification"] for it in executed],
        "needs_approval": [it["notification"] for it in dry_run],
        "blocked": [it["notification"] for it in blocked],
    }
    if write:
        try:
            BOARD_OUT.parent.mkdir(parents=True, exist_ok=True)
            tmp = BOARD_OUT.with_suffix(".tmp")
            tmp.write_text(json.dumps(board, indent=2, default=str))
            tmp.replace(BOARD_OUT)
            logger.info("[lp_rebalancer] %d items (%d exec / %d dry / %d blocked) → %s",
                        len(items), len(executed), len(dry_run), len(blocked), BOARD_OUT)
        except Exception as exc:                                      # noqa: BLE001
            logger.error("[lp_rebalancer] board write failed: %s", exc)
    return board


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="LP auto-rebalance loop (dry-run default)")
    p.add_argument("--run", action="store_true", help="run one cycle + write the board")
    p.add_argument("--no-write", action="store_true", help="don't write the board file")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    board = run_rebalance_cycle(write=not args.no_write)
    print(json.dumps(board, indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
