"""engine/strategies/lp_agile/rebalance_trigger.py — fee-cost-aware
auto-rebalance trigger engine (#109).

Designed by Yomi 2026-05-29:
- Watches every open LP position every 15 min
- For each position, asks: would rebalancing pay back within N days?
- Triggers a plan ONLY when the math says yes
- Adaptive: tightens per-pool when historical rebalances confirm payback;
  loosens when they don't

The math:

  current_daily_fees  = current_apr_pct  / 365 / 100 × position_value_usd
  expected_daily_fees = new_apr_pct      / 365 / 100 × position_value_usd_after_rebalance
  delta_daily         = expected_daily_fees - current_daily_fees
  payback_days        = est_rebalance_gas_usd / max(delta_daily, 0.0001)

If payback_days < PAYBACK_DAYS_THRESHOLD → trigger.

`new_apr_pct` is the projected APR in the new (re-centred) range. Conservative
estimate: the pool's current APR × (your_L_in_active_range / total_L_in_active_range).
For a re-centred range the second factor improves IF the price moved through the
old range; for prjx in-place modify (#108) the gas is also lower so payback faster.

Persists a journal at engine/_signals/lp_rebalance_decisions.jsonl with each
position's evaluated math + decision per tick. Future ML pass can use this to
back-test thresholds.

Standalone module — does NOT execute trades. Emits triggered plan IDs to a
queue that the existing rebalance executor consumes when LP_AUTO_EXECUTE=true.
Caller can also tap-approve via Telegram for non-auto mode.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.rebalance_trigger")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LP_LATEST = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_agile_latest.json"
DECISIONS_LOG = _REPO_ROOT / "engine" / "_signals" / "lp_rebalance_decisions.jsonl"
TRIGGER_QUEUE = _REPO_ROOT / "engine" / "_signals" / "lp_rebalance_triggers.jsonl"

# Tunable thresholds — env-overridable
PAYBACK_DAYS_THRESHOLD = float(
    os.environ.get("LP_REBAL_PAYBACK_DAYS_THRESHOLD", "7.0")
)
SOFT_PROXIMITY_PCT = float(
    os.environ.get("LP_REBAL_SOFT_PROXIMITY_PCT", "0.15")
)

# Per-protocol gas estimate for a single rebalance
# (slipstream = 4-6 tx multicall; prjx = 1 tx via execute())
# 2026-05-29 (#108): prjx revised $0.40 → $0.03 based on real tx data.
# Yomi's update tx 0x4ed841...10fc used 795,110 gas at ~1 gwei × $40 HYPE
# = $0.032. HyperEVM gas is extremely cheap — auto-rebalance economics very
# favourable. Override via LP_REBAL_EST_GAS_PRJX_USD if HYPE price moves.
EST_GAS_USD_BY_PROTOCOL = {
    "slipstream":  float(os.environ.get("LP_REBAL_EST_GAS_SLIPSTREAM_USD", "1.20")),
    "aerodrome":   float(os.environ.get("LP_REBAL_EST_GAS_SLIPSTREAM_USD", "1.20")),
    "prjx":        float(os.environ.get("LP_REBAL_EST_GAS_PRJX_USD", "0.03")),
    "uniswap_v3":  float(os.environ.get("LP_REBAL_EST_GAS_UNIV3_USD", "8.00")),
}


@dataclass
class TriggerDecision:
    ts_iso: str
    position_id: str           # pool_address|tokenId composite
    pair: str
    protocol: str
    chain: Optional[str]
    in_range: bool
    proximity_to_edge_pct: Optional[float]
    current_value_usd: float
    current_apr_pct: Optional[float]
    new_apr_pct_estimate: Optional[float]
    current_daily_fees_usd: float
    expected_daily_fees_usd: float
    delta_daily_fees_usd: float
    est_rebalance_gas_usd: float
    payback_days: Optional[float]
    trigger: Optional[str]     # "CRITICAL" / "SOFT" / "OPPORTUNITY" / None
    triggered: bool
    reason: str


def _read_positions() -> list[dict]:
    """Load open LP positions from lp_agile_latest.json."""
    if not _LP_LATEST.exists():
        logger.warning("[rebal_trigger] %s not found", _LP_LATEST)
        return []
    try:
        d = json.loads(_LP_LATEST.read_text())
        return d.get("open_positions") or []
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("[rebal_trigger] failed to parse %s: %s", _LP_LATEST, exc)
        return []


def _classify_basic(position: dict) -> tuple[Optional[str], Optional[float]]:
    """Return (trigger_class, proximity_to_edge_pct).

    CRITICAL: out of range
    SOFT: proximity to edge < SOFT_PROXIMITY_PCT of range width
    Else: None (no trigger from range alone — defer to fee-cost math)
    """
    in_range = position.get("in_range", True)
    if not in_range:
        return "CRITICAL", 0.0

    pool_price = position.get("pool_price_now") or position.get("current_price_usd")
    rl = position.get("range_low") or position.get("price_lower")
    rh = position.get("range_high") or position.get("price_upper")
    if pool_price and rl and rh and rh > rl:
        width = rh - rl
        proximity = min(pool_price - rl, rh - pool_price) / width
        if proximity < SOFT_PROXIMITY_PCT:
            return "SOFT", proximity
        return None, proximity
    return None, None


def _estimate_new_apr(position: dict) -> Optional[float]:
    """Conservative projection of APR in a re-centred new range.

    Without historical pool-distribution data, assume the new range has the
    same fee APR as the current pool (i.e. position-side scaling factor = 1
    after rebalance). Caller can refine with a per-pool model later.
    """
    current_apr = position.get("current_apr_pct")
    if current_apr is None:
        return None
    try:
        return float(current_apr)
    except Exception:                                           # noqa: BLE001
        return None


def evaluate_position(position: dict) -> TriggerDecision:
    """Run the trigger math on one position. Returns the decision row.
    Never raises — failures produce a decision with trigger=None + reason.
    """
    ts = datetime.now(timezone.utc).isoformat()
    pair = position.get("pair") or "?"
    proto = (position.get("protocol") or "").lower()
    chain = position.get("chain")
    pool_addr = position.get("pool_address") or "?"
    token_id = position.get("nft_token_id") or position.get("token_id") or "?"
    pos_id = f"{pool_addr}|{token_id}"
    value_usd = float(position.get("value_usd") or 0)

    try:
        trig_class, proximity = _classify_basic(position)
        cur_apr = position.get("current_apr_pct")
        new_apr = _estimate_new_apr(position)
        cur_daily = ((cur_apr or 0) / 365 / 100) * value_usd
        new_daily = ((new_apr or 0) / 365 / 100) * value_usd
        delta_daily = new_daily - cur_daily

        gas_usd = EST_GAS_USD_BY_PROTOCOL.get(proto, 5.0)

        payback = None
        triggered = False
        reason = "no_trigger"

        if trig_class == "CRITICAL":
            # OUT OF RANGE — earnings are $0 currently. Payback is full
            # delta (which is the full new_daily since cur_daily = 0).
            cur_daily = 0.0
            delta_daily = new_daily
            payback = gas_usd / max(delta_daily, 0.0001) if delta_daily > 0 else None
            triggered = True
            reason = (
                f"CRITICAL out-of-range, $0 fees accruing; rebalance pays back "
                f"in {payback:.1f}d" if payback else
                "CRITICAL out-of-range; payback math indeterminate"
            )
        elif trig_class == "SOFT" or (proximity is not None and proximity < SOFT_PROXIMITY_PCT):
            # SOFT: near edge. Fire if expected payback < threshold.
            if delta_daily > 0:
                payback = gas_usd / delta_daily
                if payback < PAYBACK_DAYS_THRESHOLD:
                    triggered = True
                    reason = (
                        f"SOFT near-edge (proximity={proximity:.1%}); payback "
                        f"{payback:.1f}d < threshold {PAYBACK_DAYS_THRESHOLD}d"
                    )
                else:
                    reason = (
                        f"SOFT near-edge but payback {payback:.1f}d "
                        f">= threshold {PAYBACK_DAYS_THRESHOLD}d — wait"
                    )
            else:
                reason = (
                    f"SOFT near-edge but expected APR ({new_apr}) does not "
                    f"exceed current APR ({cur_apr}) — no payback path"
                )
        else:
            prox_str = f"{proximity:.1%}" if proximity is not None else "n/a"
            reason = f"healthy (proximity={prox_str}); no rebalance"

        return TriggerDecision(
            ts_iso=ts,
            position_id=pos_id,
            pair=pair,
            protocol=proto,
            chain=chain,
            in_range=bool(position.get("in_range", True)),
            proximity_to_edge_pct=proximity,
            current_value_usd=value_usd,
            current_apr_pct=cur_apr,
            new_apr_pct_estimate=new_apr,
            current_daily_fees_usd=round(cur_daily, 4),
            expected_daily_fees_usd=round(new_daily, 4),
            delta_daily_fees_usd=round(delta_daily, 4),
            est_rebalance_gas_usd=gas_usd,
            payback_days=round(payback, 2) if payback is not None else None,
            trigger=trig_class,
            triggered=triggered,
            reason=reason,
        )
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("[rebal_trigger] evaluate failed for %s: %s", pos_id, exc)
        return TriggerDecision(
            ts_iso=ts, position_id=pos_id, pair=pair, protocol=proto, chain=chain,
            in_range=False, proximity_to_edge_pct=None,
            current_value_usd=value_usd, current_apr_pct=None,
            new_apr_pct_estimate=None,
            current_daily_fees_usd=0.0, expected_daily_fees_usd=0.0,
            delta_daily_fees_usd=0.0, est_rebalance_gas_usd=0.0,
            payback_days=None, trigger=None, triggered=False,
            reason=f"eval_error: {type(exc).__name__}: {exc}",
        )


def _append_journal(decision: TriggerDecision) -> None:
    try:
        DECISIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DECISIONS_LOG.open("a") as f:
            f.write(json.dumps(asdict(decision), default=str) + "\n")
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("[rebal_trigger] journal write failed: %s", exc)


def _enqueue_trigger(decision: TriggerDecision) -> None:
    """Append triggered position to the executor queue. Idempotent —
    re-trigger of same position_id within cooldown is harmless because the
    executor de-dups on position_id + recency."""
    try:
        TRIGGER_QUEUE.parent.mkdir(parents=True, exist_ok=True)
        with TRIGGER_QUEUE.open("a") as f:
            f.write(json.dumps({
                "enqueued_at_iso": decision.ts_iso,
                "position_id": decision.position_id,
                "pair": decision.pair,
                "protocol": decision.protocol,
                "trigger": decision.trigger,
                "payback_days": decision.payback_days,
                "reason": decision.reason,
            }, default=str) + "\n")
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("[rebal_trigger] queue write failed: %s", exc)


def run_once() -> dict:
    """One tick: evaluate every position, log decisions, enqueue triggers.
    Returns summary stats."""
    positions = _read_positions()
    stats = {
        "n_positions": len(positions),
        "n_triggered": 0,
        "n_critical": 0,
        "n_soft": 0,
        "n_healthy": 0,
        "decisions": [],
    }
    for p in positions:
        d = evaluate_position(p)
        _append_journal(d)
        stats["decisions"].append({
            "position_id": d.position_id, "pair": d.pair,
            "trigger": d.trigger, "triggered": d.triggered,
            "payback_days": d.payback_days, "reason": d.reason[:160],
        })
        if d.trigger == "CRITICAL":
            stats["n_critical"] += 1
        elif d.trigger == "SOFT":
            stats["n_soft"] += 1
        else:
            stats["n_healthy"] += 1
        if d.triggered:
            stats["n_triggered"] += 1
            _enqueue_trigger(d)
    logger.info(
        "[rebal_trigger] %d positions: %d critical, %d soft, %d healthy, "
        "%d triggered",
        stats["n_positions"], stats["n_critical"], stats["n_soft"],
        stats["n_healthy"], stats["n_triggered"],
    )
    return stats


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    s = run_once()
    print(json.dumps(s, indent=2, default=str))
