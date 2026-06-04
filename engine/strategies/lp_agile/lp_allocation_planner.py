"""engine/strategies/lp_agile/lp_allocation_planner.py — LP-REVAMP: capital-tiered allocation planner.

ADVISORY ONLY (both chains). Answers "given the capital in this wallet, how many
pools should it be in, which ones, and what's the next move as capital grows?" —
a staged plan that's ready to act on the moment funds arrive. It does NOT execute;
on Base it can later feed the auto-executor, on HyperEVM it stays a hand-guide.

The rule (Yomi-agreed):
  n_pools = clamp( floor(lp_capital / per_pool_floor), 1, max_pools )
  • per_pool_floor is PER-CHAIN (gas differs): base $250, hyperevm $100.
  • you ALWAYS hold ≥1 pool even below the floor (a small single pool is fine);
    the floor only decides when a SECOND/THIRD pool is justified.
  • max_pools = 3 (cap smart-contract surface + rebalance gas + monitoring load).
Distribution across the target pools is IL-haircut-APR-weighted but clamped so no
single pool exceeds per_pool_max_pct of LP capital (diversification) or a small %
of the pool's TVL (anti-dilution). Reach the target via INFLOWS first — never
churn a live position just to hit a count.

Inputs (read-only): ops/pwa/serve/lp_report.json (positions + venue APR + DeFiLlama
discovery) and ops/opportunities/lp_wallet_balance.json (idle, per chain). Pure
given those → unit-tested. Publishes ops/pwa/serve/lp_allocation_plan.json.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.lp_allocation_planner")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REPORT = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_report.json"
_WALLET = _REPO_ROOT / "ops" / "opportunities" / "lp_wallet_balance.json"
BOARD_OUT = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_allocation_plan.json"

_PER_POOL_FLOOR = {"base": 250.0, "hyperevm": 100.0}   # per-chain, gas-justified
_CHAIN_ALIASES = {"base": {"base"},
                  "hyperevm": {"hyperevm", "hyperliquid", "hyperliquid l1", "hyperliquidl1"}}


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _floor_for(chain: str) -> float:
    env = os.environ.get(f"LP_PER_POOL_FLOOR_USD_{chain.upper()}")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    return _PER_POOL_FLOOR.get(chain, _f("LP_PER_POOL_FLOOR_USD", 250.0))


def _norm(s: str) -> frozenset:
    return frozenset(p for p in re.split(r"[-/ +]", (s or "").upper()) if p)


def _eff_apr(c: dict) -> float:
    a = float(c.get("apy") or c.get("apy_pct") or 0)
    return a * 0.6 if str(c.get("il_risk") or "").lower() == "yes" else a


def plan_for_chain(chain: str, *, positions: list, idle_deployable_usd: float,
                   candidates: list) -> dict:
    """Pure per-chain allocation plan."""
    floor = _floor_for(chain)
    max_pools = int(_f("LP_MAX_POOLS", 3))
    per_pool_max_pct = _f("LP_PER_POOL_MAX_PCT", 60.0) / 100.0
    depth_cap_pct = _f("LP_POOL_DEPTH_CAP_PCT", 2.0) / 100.0

    deployed = sum(float(p.get("value_usd") or 0) for p in positions)
    lp_capital = round(deployed + max(0.0, idle_deployable_usd), 2)

    # n target — always ≥1, capped at max_pools
    n_target = max(1, min(max_pools, int(lp_capital // floor)))

    # universe = held pools (with their venue APR) + safe discovery candidates, deduped
    uni: dict = {}
    for p in positions:
        key = _norm(p.get("pair"))
        uni[key] = {"pair": p.get("pair"), "project": p.get("protocol"),
                    "apy": p.get("venue_apy_pct"), "il_risk": None,
                    "tvl_usd": None, "held": True}
    for c in candidates:
        key = _norm(c.get("symbol"))
        if key in uni:
            uni[key].setdefault("tvl_usd", c.get("tvl_usd"))
            if uni[key].get("apy") is None:
                uni[key]["apy"] = c.get("apy")
            continue
        uni[key] = {"pair": c.get("symbol"), "project": c.get("project"),
                    "apy": c.get("apy"), "il_risk": c.get("il_risk"),
                    "tvl_usd": c.get("tvl_usd"), "held": False}
    ranked = sorted([u for u in uni.values() if (u.get("apy") or 0) > 0],
                    key=_eff_apr, reverse=True)
    target_pools = ranked[:n_target]

    # weights — APR-weighted, then WATER-FILL the per-pool cap (clamp the over-cap
    # pools and redistribute the excess to the under-cap ones; renormalizing would
    # re-inflate a capped pool, so we iterate instead). Cap only binds when >1 pool.
    weights = [_eff_apr(t) for t in target_pools]
    wsum = sum(weights) or 1.0
    pcts = [w / wsum for w in weights]
    if n_target > 1:
        for _ in range(12):
            over = [i for i, p in enumerate(pcts) if p > per_pool_max_pct + 1e-9]
            if not over:
                break
            excess = sum(pcts[i] - per_pool_max_pct for i in over)
            for i in over:
                pcts[i] = per_pool_max_pct
            under = [i for i in range(len(pcts)) if pcts[i] < per_pool_max_pct - 1e-9]
            usum = sum(pcts[i] for i in under) or 1.0
            for i in under:
                pcts[i] += excess * (pcts[i] / usum)
    target_alloc = []
    for t, pct in zip(target_pools, pcts):
        usd = round(lp_capital * pct, 2)
        cap_note = None
        if t.get("tvl_usd"):
            depth_max = depth_cap_pct * float(t["tvl_usd"])
            if usd > depth_max:
                usd = round(depth_max, 2)
                cap_note = f"depth-capped at {depth_cap_pct*100:.0f}% of pool TVL"
        target_alloc.append({
            "pair": t["pair"], "project": t["project"],
            "apy_pct": round(float(t["apy"] or 0), 2),
            "il_risk": t.get("il_risk"), "held": t.get("held", False),
            "target_pct": round(pct * 100, 1), "target_usd": usd,
            "note": cap_note,
        })

    held_keys = {_norm(p.get("pair")) for p in positions}
    target_keys = {_norm(t["pair"]) for t in target_pools}
    to_open = [t for t in target_alloc if _norm(t["pair"]) not in held_keys]
    n_current = len(positions)

    # staged next action — inflow-first, threshold-aware
    next_pool_threshold = (n_current + 1) * floor
    if n_current < n_target and to_open:
        nxt = to_open[0]
        action = (f"Capital supports {n_target} pool(s). Open pool #{n_current+1}: "
                  f"{nxt['pair']} ({nxt['project']}, {nxt['apy_pct']:.0f}% APR) — "
                  f"fund it from idle/new capital (~${nxt['target_usd']:.0f}).")
        status = "expand"
    elif n_current > n_target:
        action = (f"Holding {n_current} pools but capital (${lp_capital:.0f}) only "
                  f"justifies {n_target} at the ${floor:.0f} floor — consider "
                  f"consolidating the weakest (subject to migration cost).")
        status = "consolidate"
    elif n_target < max_pools:
        need = round(next_pool_threshold - lp_capital, 2)
        action = (f"Balanced at {n_target} pool(s). Add ~${need:.0f} more "
                  f"(→ ${next_pool_threshold:.0f}) to justify pool #{n_target+1}.")
        status = "balanced_room_to_grow"
    else:
        action = (f"At max {max_pools} pools. New capital tops up the existing "
                  f"target weights (no new pools).")
        status = "balanced_at_max"

    return {
        "chain": chain,
        "per_pool_floor_usd": floor,
        "max_pools": max_pools,
        "lp_capital_usd": lp_capital,
        "deployed_usd": round(deployed, 2),
        "idle_deployable_usd": round(max(0.0, idle_deployable_usd), 2),
        "n_current": n_current,
        "n_target": n_target,
        "next_pool_threshold_usd": round(next_pool_threshold, 2),
        "status": status,
        "next_action": action,
        "target_allocation": target_alloc,
        "current_pools": [{"pair": p.get("pair"), "value_usd": round(float(p.get("value_usd") or 0), 2)}
                          for p in positions],
        "pools_to_open": [t["pair"] for t in to_open],
    }


def _idle_deployable(chain: str, wallet: dict) -> float:
    """Idle (non-deployed) USD on chain, minus the gas reserve we must keep."""
    try:
        sub = float(((wallet.get("by_chain") or {}).get(chain) or {}).get("subtotal_usd") or 0)
    except Exception:
        sub = 0.0
    gas_reserve = _f("LP_MIN_GAS_RESERVE_USD", 8.0)
    return max(0.0, sub - gas_reserve)


def run(*, write: bool = True) -> dict:
    try:
        report = json.loads(_REPORT.read_text())
    except Exception:
        report = {"positions": [], "discovery": {}}
    try:
        wallet = json.loads(_WALLET.read_text())
    except Exception:
        wallet = {}

    positions = report.get("positions") or []
    disc = ((report.get("discovery") or {}).get("top_all_chains")) or []

    plans = []
    for chain in ("base", "hyperevm"):
        aliases = _CHAIN_ALIASES[chain]
        chain_positions = [p for p in positions if (p.get("chain") or "").lower() in aliases]
        chain_cands = [c for c in disc if (c.get("chain") or "").lower() in aliases]
        plans.append(plan_for_chain(
            chain, positions=chain_positions,
            idle_deployable_usd=_idle_deployable(chain, wallet),
            candidates=chain_cands))

    board = {
        "generated_at_iso": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(),
        "advisory_only": True,
        "plans": plans,
        "notifications": [
            {"chain": pl["chain"], "severity": "low" if pl["status"].startswith("balanced") else "medium",
             "event": f"{pl['chain']}: ${pl['lp_capital_usd']:.0f} → {pl['n_current']}/{pl['n_target']} pools",
             "action": pl["next_action"]}
            for pl in plans
        ],
    }
    if write:
        try:
            BOARD_OUT.parent.mkdir(parents=True, exist_ok=True)
            tmp = BOARD_OUT.with_suffix(".tmp")
            tmp.write_text(json.dumps(board, indent=2, default=str))
            tmp.replace(BOARD_OUT)
            logger.info("[lp_allocation] plan published → %s", BOARD_OUT)
        except Exception as exc:                                      # noqa: BLE001
            logger.error("[lp_allocation] publish failed: %s", exc)
    return board


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Capital-tiered LP allocation planner (advisory)")
    p.add_argument("--no-write", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(json.dumps(run(write=not args.no_write), indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
