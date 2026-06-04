"""engine/strategies/lp_agile/lp_guardrails.py — LP auto-execution safety spine.

LP-REVAMP P5 (2026-06-01). Full-auto signing on a live wallet is only sane behind
hard limits. EVERYTHING defaults to SAFE: auto-execute is OFF (dry-run) until the
operator flips it on, per chain, after seeing it run. The auto-rebalancer (P3) and
the prjx zap (P4) MUST call preflight() before signing; the existing mint executor
calls it too.

Controls (env, all overridable):
  LP_AUTO_EXECUTE            "0"  — master switch. Off → engine computes + logs the
                                    action it WOULD take, signs nothing.
  LP_AUTO_EXECUTE_CHAINS     "base" — comma list of chains cleared for auto-signing
                                    (e.g. "base,hyperevm"). A chain not listed →
                                    dry-run even when the master switch is on.
  LP_MAX_TX_USD              "300" — reject any single action above this notional.
  LP_MAX_ACTIONS_PER_DAY     "8"   — cap actions/day (runaway-loop guard).
  LP_MAX_USD_PER_DAY         "1000"— cap total notional auto-deployed per day.
  LP_MAX_SLIPPAGE_PCT        "1.0" — reject swaps/zaps above this slippage.
  LP_EXECUTABLE_PROTOCOLS    "slipstream" — protocols cleared to mint (prjx added
                                    after its zap signer is verified).
Daily counters persist in engine/_state/lp_action_ledger.json (resets each UTC day).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.lp_guardrails")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LEDGER = _REPO_ROOT / "engine" / "_state" / "lp_action_ledger.json"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


_KILL_FILE = _REPO_ROOT / "engine" / "_state" / "lp_kill_switch"


def kill_switch_active() -> bool:
    """Master STOP. Active if env LP_KILL_SWITCH is truthy OR the kill file exists.
    Either halts ALL LP auto-execution instantly (operator can drop the file from
    PWA/Telegram or shell)."""
    if _env("LP_KILL_SWITCH", "0").lower() in ("1", "true", "yes"):
        return True
    try:
        return _KILL_FILE.exists()
    except Exception:
        return False


def is_auto_execute_enabled(chain: Optional[str] = None) -> bool:
    """Master switch AND per-chain clearance. Default OFF → dry-run."""
    on = _env("LP_AUTO_EXECUTE", "0").lower() in ("1", "true", "yes")
    if not on:
        return False
    if chain is None:
        return True
    cleared = {c.strip().lower() for c in _env("LP_AUTO_EXECUTE_CHAINS", "base").split(",") if c.strip()}
    return (chain or "").lower() in cleared


def _executable_protocols() -> set:
    return {p.strip().lower() for p in _env("LP_EXECUTABLE_PROTOCOLS", "slipstream").split(",") if p.strip()}


_WALLET_BAL = _REPO_ROOT / "ops" / "opportunities" / "lp_wallet_balance.json"


def _wallet_gas_usd(chain: str) -> Optional[float]:
    """Native-ETH gas balance (USD) on `chain` from the wallet-balance feed.
    Returns None if unknown (treated as 'cannot confirm' → fail-safe block)."""
    try:
        d = json.loads(_WALLET_BAL.read_text())
        toks = (((d.get("by_chain") or {}).get((chain or "").lower()) or {}).get("tokens") or {})
        eth = toks.get("ETH") or {}
        return float(eth.get("usd")) if eth.get("usd") is not None else None
    except Exception:
        return None


def gas_reserve_ok(chain: str) -> tuple:
    """(ok, gas_usd). Autonomous rebalancing that can't pay for its own exit can
    strand capital as loose tokens. Require a native-ETH float >= LP_MIN_GAS_
    RESERVE_USD before any auto-execute. Unknown balance → NOT ok (fail-safe)."""
    floor = _f("LP_MIN_GAS_RESERVE_USD", 8.0)
    gas = _wallet_gas_usd(chain)
    if gas is None:
        return False, -1.0
    return gas >= floor, gas


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _read_ledger() -> dict:
    try:
        d = json.loads(_LEDGER.read_text())
        if d.get("date") == _utc_today():
            return d
    except Exception:
        pass
    return {"date": _utc_today(), "count": 0, "usd": 0.0}


def _write_ledger(d: dict) -> None:
    try:
        _LEDGER.parent.mkdir(parents=True, exist_ok=True)
        tmp = _LEDGER.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, default=str))
        tmp.replace(_LEDGER)
    except Exception as exc:
        logger.warning("[lp_guardrails] ledger write failed: %s", exc)


def preflight(*, protocol: str, chain: str, notional_usd: float,
              slippage_pct: Optional[float] = None, action: str = "mint") -> tuple:
    """Return (ok, reason). Checks (independent of the dry-run switch — callers
    check is_auto_execute_enabled separately): protocol allowlist, per-tx cap,
    per-day count + USD caps, slippage cap. Does NOT mutate the ledger — call
    record_action() only AFTER a live action actually fires."""
    proto = (protocol or "").lower()
    if proto not in _executable_protocols():
        return False, f"protocol {proto!r} not in LP_EXECUTABLE_PROTOCOLS"

    max_tx = _f("LP_MAX_TX_USD", 300.0)
    if notional_usd > max_tx:
        return False, f"notional ${notional_usd:.2f} > per-tx cap ${max_tx:.2f}"

    if slippage_pct is not None:
        max_slip = _f("LP_MAX_SLIPPAGE_PCT", 1.0)
        if slippage_pct > max_slip:
            return False, f"slippage {slippage_pct:.2f}% > cap {max_slip:.2f}%"

    led = _read_ledger()
    max_actions = int(_f("LP_MAX_ACTIONS_PER_DAY", 8))
    max_usd_day = _f("LP_MAX_USD_PER_DAY", 1000.0)
    if led["count"] >= max_actions:
        return False, f"daily action cap reached ({led['count']}/{max_actions})"
    if led["usd"] + notional_usd > max_usd_day:
        return False, (f"daily USD cap: ${led['usd']:.0f}+${notional_usd:.0f} "
                       f"> ${max_usd_day:.0f}")
    return True, "ok"


def record_action(notional_usd: float) -> None:
    """Increment the daily ledger AFTER a live action fires."""
    led = _read_ledger()
    led["count"] += 1
    led["usd"] = float(led["usd"]) + float(notional_usd)
    _write_ledger(led)
    logger.info("[lp_guardrails] action recorded: day total %d actions / $%.2f",
                led["count"], led["usd"])


def gate(*, protocol: str, chain: str, notional_usd: float,
         slippage_pct: Optional[float] = None, action: str = "mint") -> dict:
    """One-call decision for executors. Returns {mode, ok, reason} where mode is
    'dry_run' (compute+log, don't sign), 'execute' (cleared to sign), or
    'blocked' (a hard rail failed)."""
    if kill_switch_active():
        return {"mode": "blocked", "ok": False,
                "reason": "LP KILL SWITCH active — all auto-execution halted"}
    if not is_auto_execute_enabled(chain):
        return {"mode": "dry_run", "ok": False,
                "reason": f"LP_AUTO_EXECUTE off for chain {chain!r} — dry-run only"}
    gas_ok, gas_usd = gas_reserve_ok(chain)
    if not gas_ok:
        floor = _f("LP_MIN_GAS_RESERVE_USD", 8.0)
        detail = (f"unknown (no wallet-balance feed)" if gas_usd < 0
                  else f"${gas_usd:.2f}")
        return {"mode": "blocked", "ok": False,
                "reason": f"gas reserve {detail} < ${floor:.0f} on {chain} — "
                          f"fund the ETH float before auto-execute"}
    ok, reason = preflight(protocol=protocol, chain=chain, notional_usd=notional_usd,
                           slippage_pct=slippage_pct, action=action)
    return {"mode": "execute" if ok else "blocked", "ok": ok, "reason": reason}
