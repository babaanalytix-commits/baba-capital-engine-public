"""engine/strategies/lp_agile/dynamic_range_proposer.py — propose
asymmetric range adjusts based on price action direction.

Combines:
  1. price_action_analyzer view (direction + expected drift + vol)
  2. Current position state (range, value, fee tier)
  3. Cost/benefit math (gas + IL vs expected fee uplift)

Outputs a RangeProposal that the dispatcher can either execute (HyperEVM)
or alert on (Base) based on chain-specific posture.

Yomi 2026-06-04 #420: "We can optimise returns by constantly monitoring
price action … dynamically adjust the range to optimise returns, not
necessarily waiting till it gets out of range."

Cost model:
  - chain gas cost (HyperEVM ~$0.001, Base ~$0.30-0.60)
  - IL cost: re-balancing tokens from old range distribution to new range
    inventory mix at current price
  - swap cost: 0.1-0.3% for the rebalance swap
  - fee uplift: estimated as new_fees_24h - current_fees_24h

Decision rule:
  payback_days = total_cost / max(fee_uplift_daily, 0.001)
  Fire iff payback_days < THRESHOLD (per chain)
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from engine.strategies.lp_agile.price_action_analyzer import (
    PriceActionView, analyze_lp_pair,
)

logger = logging.getLogger("engine.strategies.lp_agile.dynamic_range_proposer")

_REPO = Path(__file__).resolve().parents[3]


@dataclass
class RangeProposal:
    """Proposal to adjust an LP position's range."""
    nft_token_id: int
    pair: str
    chain: str
    protocol: str
    current_price: float
    current_tick_lower: int
    current_tick_upper: int
    current_lower_price: float
    current_upper_price: float
    new_tick_lower: int
    new_tick_upper: int
    new_lower_price: float
    new_upper_price: float
    expected_uplift_pct: float
    estimated_gas_usd: float
    estimated_il_cost_usd: float
    estimated_swap_cost_usd: float
    total_cost_usd: float
    fee_uplift_daily_usd: float
    payback_days: float
    threshold_days: float
    fire: bool                              # True if cost/benefit passes
    block_reason: Optional[str] = None
    price_action: Optional[dict] = None     # PriceActionView snapshot
    raw: dict = field(default_factory=dict)


# Per-chain cost knobs
_CHAIN_DEFAULTS = {
    "hyperevm": {
        "gas_usd": float(os.environ.get("LP_DYN_GAS_USD_HYPEREVM", "0.005")),
        "swap_cost_bps": float(os.environ.get("LP_DYN_SWAP_BPS_HYPEREVM", "30")),
        "payback_threshold_days": float(
            os.environ.get("LP_DYN_PAYBACK_DAYS_HYPEREVM", "0.5")
        ),  # 12h payback OK on cheap chain
        "min_confidence": float(
            os.environ.get("LP_DYN_MIN_CONFIDENCE_HYPEREVM", "0.35")
        ),
        "min_value_usd": float(
            os.environ.get("LP_DYN_MIN_VALUE_USD_HYPEREVM", "75")
        ),
    },
    "base": {
        "gas_usd": float(os.environ.get("LP_DYN_GAS_USD_BASE", "0.50")),
        "swap_cost_bps": float(os.environ.get("LP_DYN_SWAP_BPS_BASE", "10")),
        "payback_threshold_days": float(
            os.environ.get("LP_DYN_PAYBACK_DAYS_BASE", "5.0")
        ),  # need 5d payback to justify the gas
        "min_confidence": float(
            os.environ.get("LP_DYN_MIN_CONFIDENCE_BASE", "0.5")
        ),
        "min_value_usd": float(
            os.environ.get("LP_DYN_MIN_VALUE_USD_BASE", "150")
        ),
    },
}


def _chain_config(chain: str) -> dict:
    return _CHAIN_DEFAULTS.get(chain.lower(), _CHAIN_DEFAULTS["base"])


def _price_to_tick(price: float, tick_spacing: int = 1) -> int:
    """Convert price → Uniswap V3 tick, rounded to spacing."""
    if price <= 0:
        return 0
    raw = math.log(price) / math.log(1.0001)
    return int(round(raw / tick_spacing)) * tick_spacing


def _tick_to_price(tick: int) -> float:
    return 1.0001 ** tick


def _compute_asymmetric_range(
    *, current_price: float, view: PriceActionView,
    base_width_pct: float = 0.10,
) -> tuple[float, float]:
    """Compute (lower_price, upper_price) shifted toward expected drift.

    Width is sized to expected vol + base_width.
    Center is shifted by expected drift × confidence.

    Example: price=$50, drift=+3%, vol=4%, confidence=0.7
      width_pct = 0.10 + 4% × 2 = 18% total
      center_shift = 3% × 0.7 = 2.1% above current
      → range: $50 × (1 + 0.021) × [1 - 0.09, 1 + 0.09]
              = $50.94, $55.65 (upper), $46.36 (lower)
      → asymmetric: more space upward
    """
    # Total half-width = base + (vol × 2σ scaling × confidence)
    half_width_pct = base_width_pct / 2 + (
        view.vol_pct_24h * 2 * max(view.confidence, 0.2)
    )
    # Shift center based on expected direction × confidence
    center_shift_pct = (
        view.expected_drift_pct_24h / 100 * view.confidence
    )
    center = current_price * (1 + center_shift_pct)
    lower = center * (1 - half_width_pct)
    upper = center * (1 + half_width_pct)
    return lower, upper


def _estimate_il_cost(
    *, current_price: float, current_lower: float, current_upper: float,
    new_lower: float, new_upper: float, position_value_usd: float,
) -> float:
    """Rough IL estimate when shifting range.

    Approximation: portion of position that needs to be swapped is roughly
    the "imbalance gap" between old and new inventory mix at current price.
    For concentrated liq, when we re-center, ~|drift_pct| of value gets
    swapped. Use mid-points of ranges.
    """
    old_mid = (current_lower + current_upper) / 2
    new_mid = (new_lower + new_upper) / 2
    if old_mid <= 0:
        return 0.0
    shift_pct = abs(new_mid - old_mid) / old_mid
    # ~half the position rebalances; assume 0.2% slippage on that amount
    swap_amount = position_value_usd * min(shift_pct, 0.5)
    return swap_amount * 0.002


def propose_for_position(
    *,
    nft_token_id: int,
    pair: str,
    chain: str,
    protocol: str,
    current_price: float,
    current_tick_lower: int,
    current_tick_upper: int,
    position_value_usd: float,
    fee_tier_bps: float,
    realized_apr_pct: float,
    tick_spacing: int = 1,
    view: Optional[PriceActionView] = None,
    current_lower_price_override: Optional[float] = None,
    current_upper_price_override: Optional[float] = None,
) -> RangeProposal:
    """Produce a proposal for adjusting one position's range.

    `current_lower/upper_price_override` lets the dispatcher pass in the
    snapshot's range_low/range_high (already in the same unit as
    pool_price_now). Required for mixed-decimal pairs where tick→price math
    needs decimal scaling we don't apply here.
    """
    cfg = _chain_config(chain)

    # Gate: position too small to bother
    if position_value_usd < cfg["min_value_usd"]:
        return RangeProposal(
            nft_token_id=nft_token_id, pair=pair, chain=chain,
            protocol=protocol, current_price=current_price,
            current_tick_lower=current_tick_lower,
            current_tick_upper=current_tick_upper,
            current_lower_price=_tick_to_price(current_tick_lower),
            current_upper_price=_tick_to_price(current_tick_upper),
            new_tick_lower=current_tick_lower,
            new_tick_upper=current_tick_upper,
            new_lower_price=_tick_to_price(current_tick_lower),
            new_upper_price=_tick_to_price(current_tick_upper),
            expected_uplift_pct=0.0,
            estimated_gas_usd=cfg["gas_usd"],
            estimated_il_cost_usd=0.0,
            estimated_swap_cost_usd=0.0,
            total_cost_usd=cfg["gas_usd"],
            fee_uplift_daily_usd=0.0,
            payback_days=999.0,
            threshold_days=cfg["payback_threshold_days"],
            fire=False,
            block_reason=(
                f"position too small (${position_value_usd:.2f} < "
                f"${cfg['min_value_usd']:.0f} min)"
            ),
        )

    # Get price action view if not provided
    if view is None:
        view = analyze_lp_pair(pair)
    if view is None:
        return RangeProposal(
            nft_token_id=nft_token_id, pair=pair, chain=chain,
            protocol=protocol, current_price=current_price,
            current_tick_lower=current_tick_lower,
            current_tick_upper=current_tick_upper,
            current_lower_price=_tick_to_price(current_tick_lower),
            current_upper_price=_tick_to_price(current_tick_upper),
            new_tick_lower=current_tick_lower,
            new_tick_upper=current_tick_upper,
            new_lower_price=_tick_to_price(current_tick_lower),
            new_upper_price=_tick_to_price(current_tick_upper),
            expected_uplift_pct=0.0,
            estimated_gas_usd=cfg["gas_usd"],
            estimated_il_cost_usd=0.0,
            estimated_swap_cost_usd=0.0,
            total_cost_usd=cfg["gas_usd"],
            fee_uplift_daily_usd=0.0,
            payback_days=999.0,
            threshold_days=cfg["payback_threshold_days"],
            fire=False,
            block_reason="no_price_action_data",
        )

    # Confidence gate
    if view.confidence < cfg["min_confidence"]:
        return RangeProposal(
            nft_token_id=nft_token_id, pair=pair, chain=chain,
            protocol=protocol, current_price=current_price,
            current_tick_lower=current_tick_lower,
            current_tick_upper=current_tick_upper,
            current_lower_price=_tick_to_price(current_tick_lower),
            current_upper_price=_tick_to_price(current_tick_upper),
            new_tick_lower=current_tick_lower,
            new_tick_upper=current_tick_upper,
            new_lower_price=_tick_to_price(current_tick_lower),
            new_upper_price=_tick_to_price(current_tick_upper),
            expected_uplift_pct=0.0,
            estimated_gas_usd=cfg["gas_usd"],
            estimated_il_cost_usd=0.0,
            estimated_swap_cost_usd=0.0,
            total_cost_usd=cfg["gas_usd"],
            fee_uplift_daily_usd=0.0,
            payback_days=999.0,
            threshold_days=cfg["payback_threshold_days"],
            fire=False,
            block_reason=(
                f"confidence too low ({view.confidence:.2f} < "
                f"{cfg['min_confidence']:.2f})"
            ),
            price_action=view.__dict__,
        )

    # Compute proposed asymmetric range (anchored on unit-consistent current_price)
    new_lower, new_upper = _compute_asymmetric_range(
        current_price=current_price, view=view,
    )
    new_tick_lower = _price_to_tick(new_lower, tick_spacing)
    new_tick_upper = _price_to_tick(new_upper, tick_spacing)

    # For current range bounds: prefer snapshot overrides (correct for any
    # decimal config) over our tick→price helper which only works on equal-
    # decimal pairs. Mixed-decimal example: USDC(6)/cbBTC(8) → snapshot
    # range_low ≈ 1.08e-5 is correct; tick-derived would be miles off.
    if current_lower_price_override is not None and current_upper_price_override is not None:
        current_lower_price = current_lower_price_override
        current_upper_price = current_upper_price_override
    else:
        current_lower_price = _tick_to_price(current_tick_lower)
        current_upper_price = _tick_to_price(current_tick_upper)

    # Skip if proposal is barely different from current
    delta_lower_pct = (
        abs(new_lower - current_lower_price) / max(current_lower_price, 1e-9)
    )
    delta_upper_pct = (
        abs(new_upper - current_upper_price) / max(current_upper_price, 1e-9)
    )
    if delta_lower_pct < 0.01 and delta_upper_pct < 0.01:
        return RangeProposal(
            nft_token_id=nft_token_id, pair=pair, chain=chain,
            protocol=protocol, current_price=current_price,
            current_tick_lower=current_tick_lower,
            current_tick_upper=current_tick_upper,
            current_lower_price=current_lower_price,
            current_upper_price=current_upper_price,
            new_tick_lower=new_tick_lower,
            new_tick_upper=new_tick_upper,
            new_lower_price=new_lower,
            new_upper_price=new_upper,
            expected_uplift_pct=0.0,
            estimated_gas_usd=cfg["gas_usd"],
            estimated_il_cost_usd=0.0,
            estimated_swap_cost_usd=0.0,
            total_cost_usd=cfg["gas_usd"],
            fee_uplift_daily_usd=0.0,
            payback_days=999.0,
            threshold_days=cfg["payback_threshold_days"],
            fire=False,
            block_reason="proposed range nearly identical to current",
            price_action=view.__dict__,
        )

    # Estimate fee uplift: assume tighter / better-centered ranges yield
    # proportionally more fees. Simple model:
    #   uplift_factor = current_width / new_width  (tighter = more)
    #                 × (1 + 0.3 × confidence)   (rewarded for trust)
    current_width_pct = (
        (current_upper_price - current_lower_price)
        / max(current_price, 1e-9)
    )
    new_width_pct = (new_upper - new_lower) / max(current_price, 1e-9)
    width_ratio = current_width_pct / max(new_width_pct, 1e-9)
    uplift_factor = min(width_ratio * (1 + 0.3 * view.confidence), 3.0)
    expected_uplift_pct = (uplift_factor - 1.0) * 100  # %

    current_fees_daily = (
        realized_apr_pct / 100 / 365 * position_value_usd
    )
    fee_uplift_daily = current_fees_daily * (uplift_factor - 1.0)

    # Costs
    gas_cost = cfg["gas_usd"]
    il_cost = _estimate_il_cost(
        current_price=current_price,
        current_lower=current_lower_price,
        current_upper=current_upper_price,
        new_lower=new_lower,
        new_upper=new_upper,
        position_value_usd=position_value_usd,
    )
    swap_cost = position_value_usd * cfg["swap_cost_bps"] / 10000 / 2  # ~half rebalances
    total_cost = gas_cost + il_cost + swap_cost

    payback_days = (
        total_cost / max(fee_uplift_daily, 0.001) if fee_uplift_daily > 0
        else 999.0
    )

    fire = payback_days < cfg["payback_threshold_days"]
    block_reason = None
    if not fire:
        block_reason = (
            f"payback {payback_days:.2f}d ≥ threshold "
            f"{cfg['payback_threshold_days']:.2f}d "
            f"(cost ${total_cost:.3f}, uplift ${fee_uplift_daily:.3f}/d)"
        )

    return RangeProposal(
        nft_token_id=nft_token_id, pair=pair, chain=chain,
        protocol=protocol, current_price=current_price,
        current_tick_lower=current_tick_lower,
        current_tick_upper=current_tick_upper,
        current_lower_price=current_lower_price,
        current_upper_price=current_upper_price,
        new_tick_lower=new_tick_lower,
        new_tick_upper=new_tick_upper,
        new_lower_price=new_lower,
        new_upper_price=new_upper,
        expected_uplift_pct=round(expected_uplift_pct, 2),
        estimated_gas_usd=round(gas_cost, 4),
        estimated_il_cost_usd=round(il_cost, 4),
        estimated_swap_cost_usd=round(swap_cost, 4),
        total_cost_usd=round(total_cost, 4),
        fee_uplift_daily_usd=round(fee_uplift_daily, 4),
        payback_days=round(payback_days, 2),
        threshold_days=cfg["payback_threshold_days"],
        fire=fire,
        block_reason=block_reason,
        price_action=view.__dict__,
        raw={
            "current_width_pct": round(current_width_pct * 100, 2),
            "new_width_pct": round(new_width_pct * 100, 2),
            "uplift_factor": round(uplift_factor, 3),
        },
    )
