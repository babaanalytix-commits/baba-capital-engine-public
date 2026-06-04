"""engine/strategies/lp_agile/range_optimizer.py — pick range bounds.

Per spec §range-optimization:

  Balanced (default):  current_price × [1 - 2σ, 1 + 2σ]  — 95% probability range
  Aggressive (P+):     current_price × [1 - σ,  1 + σ]   — 68% prob, higher APR

Since we don't yet collect 7d candles for HyperEVM tokens, we use per-asset-class
default sigmas derived from public 30d realised vol observations. These are
revisitable via env override `LP_SIGMA_<ASSET_CLASS>`.

Output: (low_price, high_price, tick_lower, tick_upper, label) — already
aligned to the pool's tickSpacing.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from engine.data.lp_pools._evm import price_to_tick, tick_to_price
from engine.strategies.lp_agile.types import (
    AssetClass, PoolSnapshot, Protocol,
)

logger = logging.getLogger("engine.strategies.lp_agile.range_optimizer")

# 7-day realised σ proxies per asset class (fraction of price). Conservative
# defaults — operator can override via env e.g. LP_SIGMA_NATIVE_STABLE=0.20.
DEFAULT_SIGMA_7D = {
    AssetClass.STABLE_STABLE:   Decimal("0.005"),   # ~0.5%
    AssetClass.MAJOR_MAJOR:     Decimal("0.035"),   # ~3.5% (correlated)
    AssetClass.NATIVE_STABLE:   Decimal("0.12"),    # ~12% (HYPE, AERO etc.)
    AssetClass.LONGTAIL_STABLE: Decimal("0.25"),    # ~25%
}

# Default tickSpacing per protocol when the pool def doesn't supply one.
DEFAULT_TICK_SPACING = {
    Protocol.PRJX: 60,            # 0.30% pool spacing
    Protocol.UNISWAP_V3: 60,
    Protocol.SLIPSTREAM: 100,
    Protocol.AERODROME: 60,
}


@dataclass(frozen=True)
class RangeChoice:
    """The two bracket choices a subscriber can pick from."""
    balanced_low: Decimal
    balanced_high: Decimal
    balanced_tick_lower: int
    balanced_tick_upper: int
    aggressive_low: Decimal
    aggressive_high: Decimal
    aggressive_tick_lower: int
    aggressive_tick_upper: int
    sigma_used: Decimal
    notes: str

    def chosen(self, mode: str = "balanced") -> tuple[Decimal, Decimal, int, int]:
        if mode == "aggressive":
            return (self.aggressive_low, self.aggressive_high,
                    self.aggressive_tick_lower, self.aggressive_tick_upper)
        return (self.balanced_low, self.balanced_high,
                self.balanced_tick_lower, self.balanced_tick_upper)


def compute_range(snapshot: PoolSnapshot) -> Optional[RangeChoice]:
    """Compute balanced + aggressive range options for a pool.

    Returns None if the snapshot has no valid base_price.
    """
    if snapshot.base_price_usd <= 0:
        return None
    pool = snapshot.pool
    sigma_key = f"LP_SIGMA_{pool.asset_class.value.upper()}"
    sigma_source = "static_default"
    # Sigma priority (2026-05-27): env override > LIVE realised vol > static.
    #   1. env LP_SIGMA_<CLASS>  — operator's explicit pin (highest authority)
    #   2. LIVE vol from range_advisor — σ over a ~month from HL daily candles
    #      (σ_month = σ_daily·√horizon). Replaces the hardcoded 7d proxy the
    #      module's own header flagged as a stopgap. balanced=±2σ (≈95% monthly
    #      range, stays in-range longer), aggressive=±1σ (≈68%, more fees).
    #   3. DEFAULT_SIGMA_7D — fallback when candles unavailable.
    sigma = None
    sigma_raw = os.environ.get(sigma_key)
    if sigma_raw:
        try:
            sigma = Decimal(sigma_raw); sigma_source = "env_override"
        except Exception:                     # noqa: BLE001
            sigma = None
    if sigma is None:
        try:
            from engine.strategies.lp_agile.range_advisor import _vol_and_price
            base = (getattr(pool, "base_symbol", None)
                    or str(pool.pair).split("/")[0])
            live_move, _ = _vol_and_price(base)
            if live_move is not None and live_move > 0:
                sigma = Decimal(str(round(live_move, 6))); sigma_source = "live_vol"
        except Exception as _exc:             # noqa: BLE001
            logger.debug("live-vol unavailable for %s: %s", pool.id, _exc)
    if sigma is None:
        sigma = DEFAULT_SIGMA_7D.get(pool.asset_class, Decimal("0.10"))
    logger.info("[range_optimizer] %s sigma=%.4f source=%s", pool.id, float(sigma), sigma_source)

    p = snapshot.base_price_usd
    bal_low = p * (Decimal("1") - 2 * sigma)
    bal_high = p * (Decimal("1") + 2 * sigma)
    agg_low = p * (Decimal("1") - sigma)
    agg_high = p * (Decimal("1") + sigma)

    # Tick-space alignment using ACTUAL pool decimals from the snapshot
    # (per [[feedback-lp-tick-math-decimals]]). Hardcoded (18, 6) bricked
    # cbBTC/USDC (6, 8) mints — now route through snapshot decimals + stable flags.
    spacing = (snapshot.pool.tick_spacing
               or DEFAULT_TICK_SPACING.get(snapshot.pool.protocol, 60))
    dec0 = snapshot.token0_decimals
    dec1 = snapshot.token1_decimals
    is_stable_1 = snapshot.is_stable_1

    def _usd_price_to_tick(usd_per_base: Decimal) -> int:
        """USD price of the volatile asset → pool's native tick coordinate."""
        import math as _m
        if is_stable_1:
            price_human = float(usd_per_base)
        else:
            price_human = 1.0 / float(usd_per_base) if usd_per_base else 0.0
        if price_human <= 0:
            return 0
        raw_atomic = price_human * (10 ** (dec1 - dec0))
        return int(_m.log(raw_atomic) / _m.log(1.0001))

    try:
        t_a, t_b = _usd_price_to_tick(bal_low), _usd_price_to_tick(bal_high)
        bal_lo_raw, bal_hi_raw = min(t_a, t_b), max(t_a, t_b)
        t_a, t_b = _usd_price_to_tick(agg_low), _usd_price_to_tick(agg_high)
        agg_lo_raw, agg_hi_raw = min(t_a, t_b), max(t_a, t_b)

        def _align_down(t: int) -> int:
            return (t // spacing) * spacing
        def _align_up(t: int) -> int:
            return ((t + spacing - 1) // spacing) * spacing

        t_bal_low = _align_down(bal_lo_raw)
        t_bal_high = _align_up(bal_hi_raw)
        t_agg_low = _align_down(agg_lo_raw)
        t_agg_high = _align_up(agg_hi_raw)
    except Exception as e:                    # noqa: BLE001
        logger.debug("tick math failed for %s: %s", pool.id, e)
        t_bal_low = t_bal_high = t_agg_low = t_agg_high = 0

    return RangeChoice(
        balanced_low=bal_low,
        balanced_high=bal_high,
        balanced_tick_lower=t_bal_low,
        balanced_tick_upper=t_bal_high,
        aggressive_low=agg_low,
        aggressive_high=agg_high,
        aggressive_tick_lower=t_agg_low,
        aggressive_tick_upper=t_agg_high,
        sigma_used=sigma,
        notes=f"σ={sigma*100:.1f}% (class={pool.asset_class.value}, "
              f"override via env {sigma_key})",
    )


def project_il(price_change_pct: Decimal) -> Decimal:
    """Standard Uniswap V2-style IL given a price ratio change.

    IL = 2 × sqrt(r) / (1 + r) - 1
    where r = new_price / old_price.

    For concentrated liquidity within range, IL is HIGHER than v2 by roughly
    1/(1 - sqrt(range_fraction))^2 — but a v2-style approximation is fine for
    subscriber alerts (we surface concrete loss numbers, not formulas).
    """
    from decimal import getcontext
    import math
    getcontext().prec = 28
    r = float(1 + price_change_pct)
    if r <= 0:
        return Decimal("-1")
    il = 2 * math.sqrt(r) / (1 + r) - 1
    return Decimal(str(il))
