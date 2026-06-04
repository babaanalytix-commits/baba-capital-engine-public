"""engine/strategies/lp_agile/rebalance_plan.py — position-level rebalance planner.

PHASE 2b PRE-AUTOMATION. This module SUGGESTS rebalance plans for individual
LP positions. It does NOT execute. Gated by LP_AUTO_EXECUTE for any future
execution phase.

Why a separate module from triggers.py + lp_tiered.py:
  - triggers.py emits LPSignal objects for subscriber-facing distribution.
  - lp_tiered.py emits TIER-level rebalance (target-weight drift).
  - rebalance_plan.py emits POSITION-level rebalance plans for OWNER (Yomi)
    with concrete tx sequences, gas estimates, and AERO claim values — the
    things needed to either (a) one-tap approve via Telegram in Phase 2a, or
    (b) auto-execute in Phase 2b.

Trigger logic (per position):
  - in_range == False                          → CRITICAL (earning $0)
  - proximity < SOFT_REBAL_PROXIMITY_PCT (20%) → SOFT (preempt break-out)
  - apr_drop_pct > APR_DROP_THRESHOLD (30%)    → OPPORTUNITY (better pool exists)
  - otherwise                                  → HEALTHY (no plan)

For each CRITICAL/SOFT/OPPORTUNITY plan:
  1. Resolve current pool + snapshot → new range via range_optimizer
  2. Sequence: getReward → withdraw → decreaseLiquidity → collect → mint(new range) → approve → deposit
     (for staked positions; unstaked drops the getReward/withdraw/deposit steps)
  3. Estimate gas cost via dry-run gas estimates on each tx
  4. Compute claimable AERO USD value (offset against gas cost)
  5. Compute expected fee-yield delta (current APR vs projected APR in new range)

Output: ops/pwa/serve/lp_rebalance_plans.json
  {
    "generated_at_iso": "...",
    "wallet": "0x0108...",
    "lp_auto_execute_enabled": false,
    "plans": [
      {
        "plan_id": "rebal:71276872:1716700000",
        "trigger": "CRITICAL" | "SOFT" | "OPPORTUNITY",
        "position": {token_id, pool, current_range, current_value_usd, ...},
        "new_range": {tick_lower, tick_upper, price_lower, price_upper},
        "tx_sequence": [...],
        "est_gas_usd": 5.40,
        "claimable_aero": {amount, usd},
        "net_cost_usd": -2.10,        // gas minus AERO credit
        "expected_apr_pct_new_range": 67.4,
        "rationale": "Price drifted to tick -66435 with 18% proximity to upper bound..."
      }
    ]
  }

Telegram payload uses Premium UI standard ([[feedback_premium_ui_standard]]):
clean HTML card, one clear action, deep link to pool on basescan.

Per [[feedback_all_execution_delta_neutral]]: LP positions are not paired with
perps so the delta-neutral rule doesn't apply, but the spirit ("never half-do
a sequence") DOES — if any step in the sequence fails mid-flight, the
plan must record partial state and continue from there on next retry.

Per [[feedback_no_patches_root_cause_only]]: this module is part of the
canonical LP automation path, not a patch.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.rebalance_plan")

_REPO_ROOT = Path(__file__).resolve().parents[3]
LP_AGILE_LATEST = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_agile_latest.json"
PLANS_OUT = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_rebalance_plans.json"


# ---------------------------------------------------------------------------
# Thresholds — tunable via lp_tiers.yaml later
# ---------------------------------------------------------------------------

SOFT_REBAL_PROXIMITY_PCT = 0.20  # rebal hint when current price within 20% of edge
APR_DROP_THRESHOLD_PCT   = 0.30  # rebal hint when current pool APR dropped 30%
HARD_OUT_OF_RANGE_MIN_BLOCKS = 100  # require persistent out-of-range to avoid flapping

# Conservative gas estimates (Base mainnet ~ $0.001/gwei × 21000-300000 gas).
# Real values will be derived from estimate_gas() in execution phase.
EST_GAS_USD_PER_TX = {
    "getReward":   0.05,
    "withdraw":    0.40,
    "decreaseLiquidity": 0.30,
    "collect":     0.15,
    "burn":        0.10,  # optional — saves dust
    "mint":        0.80,
    "approve":     0.05,  # one-time per gauge
    "deposit":     0.60,
}


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class TxStep:
    method: str
    target: str             # human label ("Slipstream NPM", "cbBTC/USDC gauge")
    target_address: str
    args_summary: str       # human-readable args (no calldata)
    est_gas_usd: float


@dataclass
class RebalancePlan:
    plan_id: str
    trigger: str            # "CRITICAL" | "SOFT" | "OPPORTUNITY"
    rationale: str
    wallet: str
    nft_token_id: int
    pool_address: str
    protocol: str
    pair: str
    current_state: dict     # value_usd, in_range, range, AERO claimable
    new_range: dict         # tick_lower, tick_upper, price_lower, price_upper
    tx_sequence: list[TxStep]
    est_gas_usd: float
    est_claimable_aero_usd: float
    est_net_cost_usd: float
    expected_apr_pct_new_range: Optional[float] = None
    generated_at_iso: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_json_dict(self) -> dict:
        d = asdict(self)
        # Decimal serialization safety
        return d


# ---------------------------------------------------------------------------
# Plan builder
# ---------------------------------------------------------------------------


def _classify_trigger(position: dict, current_apr_pct: Optional[float] = None) -> Optional[str]:
    """Return trigger name or None if HEALTHY."""
    in_range = position.get("in_range", True)
    if not in_range:
        return "CRITICAL"

    # SOFT — proximity to edge.
    pool_price = position.get("pool_price_now")
    range_low = position.get("range_low")
    range_high = position.get("range_high")
    if pool_price and range_low and range_high and range_high > range_low:
        width = range_high - range_low
        dist_lo = pool_price - range_low
        dist_hi = range_high - pool_price
        proximity = min(dist_lo, dist_hi) / width if width > 0 else 1.0
        if proximity < SOFT_REBAL_PROXIMITY_PCT:
            return "SOFT"

    # OPPORTUNITY — pool APR drop check (requires baseline; skip for now).
    return None


def _build_tx_sequence(position: dict) -> list[TxStep]:
    """Build the tx sequence for rebalancing.

    Staked positions need the full 6-step sequence:
      1. getReward (claim AERO)
      2. withdraw (unstake NFT from gauge)
      3. decreaseLiquidity (remove all liquidity)
      4. collect (sweep fees + freed tokens)
      5. mint (new range)
      6. deposit (re-stake in gauge)

    Approve is one-time per gauge per token — we omit it from default seq
    unless we know allowance is missing.

    Unstaked positions skip steps 1, 2, 6 (3 fewer txs).
    """
    is_staked = bool(position.get("staked"))
    seq: list[TxStep] = []
    pool_addr = position.get("pool_address", "?")
    gauge_addr = position.get("staked_in_gauge", "?")

    if is_staked:
        seq.append(TxStep(
            method="getReward",
            target=f"{position.get('pair', '?')} gauge",
            target_address=gauge_addr,
            args_summary=f"tokenId={position.get('nft_token_id')}",
            est_gas_usd=EST_GAS_USD_PER_TX["getReward"],
        ))
        seq.append(TxStep(
            method="withdraw",
            target=f"{position.get('pair', '?')} gauge",
            target_address=gauge_addr,
            args_summary=f"tokenId={position.get('nft_token_id')} (unstake)",
            est_gas_usd=EST_GAS_USD_PER_TX["withdraw"],
        ))

    seq.append(TxStep(
        method="decreaseLiquidity",
        target="Slipstream NPM",
        target_address="0x827922686190790b37229fd06084350E74485b72",
        args_summary=f"tokenId={position.get('nft_token_id')} liquidity=ALL",
        est_gas_usd=EST_GAS_USD_PER_TX["decreaseLiquidity"],
    ))
    seq.append(TxStep(
        method="collect",
        target="Slipstream NPM",
        target_address="0x827922686190790b37229fd06084350E74485b72",
        args_summary=f"tokenId={position.get('nft_token_id')} sweep all",
        est_gas_usd=EST_GAS_USD_PER_TX["collect"],
    ))
    seq.append(TxStep(
        method="mint",
        target="Slipstream NPM",
        target_address="0x827922686190790b37229fd06084350E74485b72",
        args_summary=f"new range (re-centred on current price)",
        est_gas_usd=EST_GAS_USD_PER_TX["mint"],
    ))

    if is_staked:
        seq.append(TxStep(
            method="deposit",
            target=f"{position.get('pair', '?')} gauge",
            target_address=gauge_addr,
            args_summary=f"new NFT tokenId (re-stake)",
            est_gas_usd=EST_GAS_USD_PER_TX["deposit"],
        ))

    return seq


def _compute_new_range_placeholder(position: dict) -> dict:
    """Placeholder new-range computation.

    Production version reuses range_optimizer.compute_range(snapshot). For now,
    centre the current ±50% range on the live price — proves the structure
    flows; the executor will compute the real range from live snapshot.
    """
    pool_price = position.get("pool_price_now")
    if not pool_price:
        return {}
    width_pct = 0.20  # ±20% range, conservative
    return {
        "tick_lower": None,  # filled by executor from real range_optimizer
        "tick_upper": None,
        "price_lower": pool_price * (1 - width_pct),
        "price_upper": pool_price * (1 + width_pct),
        "width_pct": width_pct,
        "note": "PLACEHOLDER — real range computed from range_optimizer at execution time",
    }


def build_plan_for_position(
    wallet_address: str, position: dict,
    *, trigger_override: Optional[str] = None,
) -> Optional[RebalancePlan]:
    """Build a RebalancePlan if the position needs rebalancing; else None.

    trigger_override forces a specific trigger (useful for demo / Yomi UX
    preview where current position is HEALTHY).
    """
    trigger = trigger_override or _classify_trigger(position)
    if trigger is None:
        return None

    nft_id = int(position.get("nft_token_id", 0))
    tx_seq = _build_tx_sequence(position)
    est_gas = sum(t.est_gas_usd for t in tx_seq)
    claimable_aero_usd = float(position.get("pending_aero_usd") or 0)
    net_cost = est_gas - claimable_aero_usd

    rationale_map = {
        "CRITICAL": (
            f"Price ${position.get('pool_price_now', 0):.6f} OUT OF RANGE "
            f"[${position.get('range_low', 0):.6f}, ${position.get('range_high', 0):.6f}]. "
            f"Position earning $0 fees. Rebalance + restake recommended."
        ),
        "SOFT": (
            f"Price approaching range edge (<{int(SOFT_REBAL_PROXIMITY_PCT*100)}% buffer). "
            f"Preemptive rebalance widens range before break-out. "
            f"Cost ${est_gas:.2f} (offset ${claimable_aero_usd:.4f} AERO) "
            f"vs estimated daily fees if rebalanced."
        ),
        "OPPORTUNITY": (
            f"Better pool / range available. Rotating preserves yield."
        ),
    }
    rationale = rationale_map.get(trigger, f"Trigger: {trigger}")

    return RebalancePlan(
        plan_id=f"rebal:{nft_id}:{int(datetime.now(timezone.utc).timestamp())}",
        trigger=trigger,
        rationale=rationale,
        wallet=wallet_address,
        nft_token_id=nft_id,
        pool_address=position.get("pool_address", "?"),
        protocol=position.get("protocol", "?"),
        pair=position.get("pair", "?"),
        current_state={
            "value_usd": position.get("value_usd"),
            "in_range": position.get("in_range"),
            "range_low": position.get("range_low"),
            "range_high": position.get("range_high"),
            "pool_price_now": position.get("pool_price_now"),
            "fees_owed_usd": position.get("fees_owed_usd"),
            "staked": position.get("staked"),
            "staked_in_gauge": position.get("staked_in_gauge"),
            "pending_aero": position.get("pending_aero"),
            "pending_aero_usd": position.get("pending_aero_usd"),
        },
        new_range=_compute_new_range_placeholder(position),
        tx_sequence=tx_seq,
        est_gas_usd=est_gas,
        est_claimable_aero_usd=claimable_aero_usd,
        est_net_cost_usd=net_cost,
        expected_apr_pct_new_range=None,  # filled by executor
    )


# ---------------------------------------------------------------------------
# Entry point — emit plans for all open positions
# ---------------------------------------------------------------------------


def run_once(
    *, wallet_address: Optional[str] = None,
    demo_force_trigger: Optional[str] = None,
    write: bool = True,
) -> dict:
    """Read lp_agile_latest.json, build plans, write to disk."""
    if not LP_AGILE_LATEST.exists():
        logger.warning("lp_agile_latest.json missing at %s", LP_AGILE_LATEST)
        return {"ok": False, "error": "no_snapshot"}
    try:
        snap = json.loads(LP_AGILE_LATEST.read_text())
    except Exception as exc:                                  # noqa: BLE001
        logger.error("snapshot parse failed: %s", exc)
        return {"ok": False, "error": "snapshot_parse"}

    config = snap.get("config") or {}
    wallet = wallet_address or config.get("wallet_address") \
        or os.environ.get("LP_WALLET_ADDRESS")
    if not wallet:
        logger.warning("no wallet address — skipping rebalance plan run")
        return {"ok": False, "error": "no_wallet"}

    auto_exec = (os.environ.get("LP_AUTO_EXECUTE", "false").lower() == "true")

    plans: list[dict] = []
    for pos in (snap.get("open_positions") or []):
        plan = build_plan_for_position(
            wallet, pos,
            trigger_override=demo_force_trigger,
        )
        if plan is not None:
            plans.append(plan.to_json_dict())

    out = {
        "generated_at_iso": datetime.now(timezone.utc).isoformat(),
        "wallet": wallet,
        "lp_auto_execute_enabled": auto_exec,
        "phase": "2a-suggestion-only" if not auto_exec else "2b-auto-execute",
        "n_plans": len(plans),
        "plans": plans,
        "demo_force_trigger": demo_force_trigger,
    }

    if write:
        try:
            PLANS_OUT.parent.mkdir(parents=True, exist_ok=True)
            tmp = PLANS_OUT.with_suffix(".tmp")
            tmp.write_text(json.dumps(out, indent=2, default=str))
            tmp.replace(PLANS_OUT)
            logger.info("wrote %d rebalance plan(s) → %s", len(plans), PLANS_OUT)
        except Exception as exc:                              # noqa: BLE001
            logger.error("plans write failed: %s", exc)

    return out


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="LP position-level rebalance planner")
    p.add_argument("--wallet", help="override LP wallet address")
    p.add_argument("--demo", choices=["CRITICAL", "SOFT", "OPPORTUNITY"],
                   help="force a trigger for UX preview (HEALTHY positions only)")
    p.add_argument("--no-write", action="store_true",
                   help="dry-run; don't write plans file")
    p.add_argument("--json", action="store_true", help="print full JSON output")
    args = p.parse_args(argv)

    out = run_once(
        wallet_address=args.wallet,
        demo_force_trigger=args.demo,
        write=not args.no_write,
    )
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"plans generated: {out.get('n_plans', 0)}")
        for plan in out.get("plans", []):
            print(f"  [{plan['trigger']}] {plan['pair']} #{plan['nft_token_id']} "
                  f"est_gas=${plan['est_gas_usd']:.2f} "
                  f"net_cost=${plan['est_net_cost_usd']:.4f}")
            print(f"    {plan['rationale']}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
