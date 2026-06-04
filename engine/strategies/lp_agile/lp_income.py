"""engine/strategies/lp_agile/lp_income.py — LP-REVAMP P5 hybrid income engine.

Turns accrued fees into a decision per position: COMPOUND (reinvest, grows the
fee base), HARVEST (claim to wallet = the steady monthly income), or WAIT (fees
are still dust — a tx would cost more than it's worth).

Hybrid policy (the strategy Yomi locked in):
  • dust          fees < gas × COMPOUND_MIN_GAS_MULT      → WAIT
  • building      dust-cleared, < HARVEST_MIN_USD, in-range → COMPOUND (let it grow)
  • income-ready  fees ≥ HARVEST_MIN_USD                   → HARVEST to wallet
  • out-of-range  any size over dust                        → HARVEST (rebalancer
                  re-centres the principal; we don't compound into a dead range)

Every action is an on-chain tx, so it runs through the SAME guardrails as the
rebalancer (per-tx cap, daily caps, chain clearance, protocol allowlist) and is
DRY-RUN by default. Nothing here signs; an injected executor does, once verified.

Output: ops/pwa/serve/lp_income_board.json
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import lp_guardrails as guard

logger = logging.getLogger("engine.strategies.lp_agile.lp_income")

_REPO_ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_agile_latest.json"
BOARD_OUT = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_income_board.json"

_PROTO_CHAIN = {
    "slipstream": "base", "aerodrome": "base", "aerodrome-slipstream": "base",
    "uniswap-v3": "base", "prjx": "hyperevm", "project-x": "hyperevm",
    "hyperswap": "hyperevm",
}
# rough per-claim gas by chain (Base L2 cheap; HyperEVM cheaper)
_GAS_USD = {"base": 0.85, "hyperevm": 0.05}


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _chain_of(pos: dict) -> str:
    return (pos.get("chain") or _PROTO_CHAIN.get((pos.get("protocol") or "").lower(), "base")).lower()


def decide_for_position(pos: dict) -> dict:
    """Pure: classify one position into compound/harvest/wait with a reason."""
    pair = pos.get("pair", "?")
    chain = _chain_of(pos)
    fees = float(pos.get("fees_owed_usd") or 0.0)
    in_range = bool(pos.get("in_range", True))
    gas = _GAS_USD.get(chain, 0.85)

    compound_mult = _f("LP_COMPOUND_MIN_GAS_MULT", 3.0)
    harvest_min = _f("LP_HARVEST_MIN_USD", 25.0)

    if fees < gas * compound_mult:
        action, reason = "wait", (f"fees ${fees:.2f} < {compound_mult:g}× gas "
                                  f"(${gas*compound_mult:.2f}) — let them accrue")
    elif fees >= harvest_min:
        action, reason = "harvest", (f"fees ${fees:.2f} ≥ harvest target ${harvest_min:.0f} "
                                     f"— claim to wallet as income")
    elif not in_range:
        action, reason = "harvest", (f"position out of range — claim ${fees:.2f} now; "
                                     f"rebalancer re-centres the principal")
    else:
        action, reason = "compound", (f"fees ${fees:.2f} clear gas — reinvest in-range to "
                                      f"grow the base (harvest at ${harvest_min:.0f})")
    return {
        "pair": pair, "chain": chain, "protocol": pos.get("protocol"),
        "fees_usd": round(fees, 2), "gas_usd": gas, "in_range": in_range,
        "action": action, "reason": reason,
        "nft_token_id": pos.get("nft_token_id"),
    }


def plan_income(*, positions: Optional[list[dict]] = None) -> list[dict]:
    """Decision + guardrail gate per position. Pure given `positions`."""
    if positions is None:
        try:
            positions = (json.loads(SNAPSHOT.read_text()).get("open_positions") or [])
        except Exception:
            positions = []
    items = []
    for pos in positions:
        d = decide_for_position(pos)
        if d["action"] == "wait":
            d["mode"] = "wait"
            items.append(d)
            continue
        proto = (d.get("protocol") or "").lower()
        gate = guard.gate(protocol=proto, chain=d["chain"], notional_usd=d["fees_usd"],
                          slippage_pct=None, action=d["action"])
        d["mode"] = gate["mode"]
        d["gate_reason"] = gate["reason"]
        items.append(d)
    return items


def run_income_cycle(*, executor_fn: Optional[Callable[[dict], dict]] = None,
                     write: bool = True) -> dict:
    """Detect → decide → gate → (dry-run | execute | block) → publish.

    executor_fn(decision) -> {"ok": bool, "error": str|None} is the only signer.
    When None, execute decisions resolve to blocked (no silent no-op)."""
    items = plan_income()
    income_ready = sum(i["fees_usd"] for i in items if i["action"] == "harvest")
    compounding = sum(i["fees_usd"] for i in items if i["action"] == "compound")

    for d in items:
        if d.get("mode") != "execute":
            continue
        if executor_fn is None:
            d["mode"] = "blocked"
            d["gate_reason"] = "executor not wired (claim/compound signer pending verification)"
            continue
        try:
            res = executor_fn(d)
        except Exception as exc:                                      # noqa: BLE001
            res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if res.get("ok"):
            guard.record_action(d["fees_usd"])
            d["result"] = res
        else:
            d["mode"] = "blocked"
            d["gate_reason"] = f"executor failed: {res.get('error')}"

    board = {
        "generated_at_iso": datetime.now(timezone.utc).isoformat(),
        "auto_execute_enabled": guard.is_auto_execute_enabled(),
        "harvestable_income_usd": round(income_ready, 2),
        "compounding_usd": round(compounding, 2),
        "n_harvest": sum(1 for i in items if i["action"] == "harvest"),
        "n_compound": sum(1 for i in items if i["action"] == "compound"),
        "n_wait": sum(1 for i in items if i["action"] == "wait"),
        "items": items,
    }
    if write:
        try:
            BOARD_OUT.parent.mkdir(parents=True, exist_ok=True)
            tmp = BOARD_OUT.with_suffix(".tmp")
            tmp.write_text(json.dumps(board, indent=2, default=str))
            tmp.replace(BOARD_OUT)
            logger.info("[lp_income] harvest=$%.2f compound=$%.2f (%d items) → %s",
                        income_ready, compounding, len(items), BOARD_OUT)
        except Exception as exc:                                      # noqa: BLE001
            logger.error("[lp_income] board write failed: %s", exc)
    return board


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="LP hybrid compound/harvest income (dry-run default)")
    p.add_argument("--no-write", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(json.dumps(run_income_cycle(write=not args.no_write), indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
