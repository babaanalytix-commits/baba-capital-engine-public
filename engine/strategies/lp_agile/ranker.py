"""engine/strategies/lp_agile/ranker.py — composite scoring across pools.

Score = fee_apr_norm × W_fee
      + airdrop_apr_norm × W_airdrop
      - il_risk_norm × W_il
      + tvl_depth_norm × W_tvl
      + volume_consistency_norm × W_volcon

Weights live in lp_universe.yaml `default_weights`. Each component is
normalised to [0, 1] before weighting so the math stays scale-free.

Trustless: ranker REFUSES to score a snapshot that's older than the
protocol's `protocol_freshness_max_s` window. Stale → excluded from results.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from engine.strategies.lp_agile.types import (
    AssetClass, PoolSnapshot, RankedPool,
)

logger = logging.getLogger("engine.strategies.lp_agile.ranker")


# IL risk score by asset class — higher = more IL-prone. Already in [0, 1].
IL_RISK_BY_CLASS = {
    AssetClass.STABLE_STABLE:   Decimal("0.02"),
    AssetClass.MAJOR_MAJOR:     Decimal("0.20"),
    AssetClass.NATIVE_STABLE:   Decimal("0.50"),
    AssetClass.LONGTAIL_STABLE: Decimal("0.90"),
}

# "Reference high APR" used to normalise fee_apr to [0, 1]. APR above this
# saturates the score component at 1.0. 100% APR is generous enough that the
# composite still moves with high-APR pools without one pool dominating.
FEE_APR_REFERENCE = Decimal("1.0")          # 100% APR
AIRDROP_APR_REFERENCE = Decimal("0.5")      # 50% APR speculative

# TVL depth normaliser: log10(tvl_usd) / log10(reference) capped at 1.
TVL_DEPTH_REFERENCE_USD = Decimal("100000000")  # $100M = full depth score


def rank_pools(
    snapshots: Iterable[PoolSnapshot],
    *, weights: dict,
    freshness_max_s: dict,
) -> list[RankedPool]:
    """Rank a batch of snapshots. Returns sorted descending by score.

    Excludes snapshots that are stale (per `freshness_max_s` for their protocol)
    or that have invalid TVL (=0 = no stable-leg pricing yet).
    """
    fresh: list[PoolSnapshot] = []
    for snap in snapshots:
        max_age = freshness_max_s.get(snap.pool.protocol.value)
        if max_age is None:
            logger.warning("no freshness_max_s for protocol=%s — skipping %s",
                           snap.pool.protocol.value, snap.pool.id)
            continue
        if snap.source.age_seconds() > max_age:
            logger.info("stale snapshot for %s (age=%.0fs > max=%ds) — skipping",
                        snap.pool.id, snap.source.age_seconds(), max_age)
            continue
        if snap.tvl_usd <= 0:
            logger.info("pool %s has TVL=0 (likely non-stable quote) — skipping",
                        snap.pool.id)
            continue
        fresh.append(snap)

    ranked: list[RankedPool] = []
    for snap in fresh:
        ranked.append(_score_one(snap, weights))
    ranked.sort(key=lambda r: r.score, reverse=True)
    return ranked


def _score_one(snap: PoolSnapshot, weights: dict) -> RankedPool:
    pool = snap.pool

    # fee_apr component (normalise to [0, 1])
    fee_apr = snap.fee_apr or Decimal(0)
    fee_apr_norm = min(fee_apr / FEE_APR_REFERENCE, Decimal(1))

    # airdrop component
    air = snap.speculative_airdrop_apr_est or Decimal(0)
    air_norm = min(air / AIRDROP_APR_REFERENCE, Decimal(1))

    # IL penalty by asset class
    il_norm = IL_RISK_BY_CLASS.get(pool.asset_class, Decimal("0.50"))

    # TVL depth: log10(tvl) / log10(reference), capped at 1
    if snap.tvl_usd > 0:
        tvl_log = Decimal(str(math.log10(float(snap.tvl_usd))))
        ref_log = Decimal(str(math.log10(float(TVL_DEPTH_REFERENCE_USD))))
        tvl_norm = min(tvl_log / ref_log, Decimal(1))
    else:
        tvl_norm = Decimal(0)

    # Volume consistency: 24h volume / TVL. High turnover (vol > TVL) saturates.
    if snap.tvl_usd > 0 and snap.volume_24h_usd > 0:
        turn = snap.volume_24h_usd / snap.tvl_usd
        vol_con_norm = min(turn, Decimal(1))
    else:
        vol_con_norm = Decimal(0)

    w_fee = Decimal(str(weights.get("fee_apr", 0.30)))
    w_air = Decimal(str(weights.get("airdrop_apr_est", 0.20)))
    w_il = Decimal(str(weights.get("il_risk", 0.30)))
    w_tvl = Decimal(str(weights.get("tvl_depth", 0.10)))
    w_vol = Decimal(str(weights.get("volume_consistency", 0.10)))

    score = (
        fee_apr_norm * w_fee
        + air_norm * w_air
        - il_norm * w_il
        + tvl_norm * w_tvl
        + vol_con_norm * w_vol
    )

    # Tag for the alert: what's the dominant driver?
    contributions = {
        "fee_apr": fee_apr_norm * w_fee,
        "airdrop": air_norm * w_air,
        "il_penalty": -il_norm * w_il,
        "tvl_depth": tvl_norm * w_tvl,
        "vol_consistency": vol_con_norm * w_vol,
    }
    dominant = max(contributions, key=lambda k: abs(contributions[k]))

    rationale_parts = [
        f"fee APR {float(fee_apr)*100:.1f}%",
        f"TVL ${float(snap.tvl_usd):,.0f}",
    ]
    if snap.volume_24h_usd > 0:
        rationale_parts.append(f"vol/TVL {float(vol_con_norm):.2f}")
    if air > 0:
        rationale_parts.append(f"airdrop est {float(air)*100:.0f}%")
    rationale_parts.append(f"class={pool.asset_class.value}")
    rationale = " · ".join(rationale_parts) + f" → dominant: {dominant}"

    return RankedPool(
        snapshot=snap,
        score=score,
        fee_apr_component=fee_apr_norm * w_fee,
        airdrop_component=air_norm * w_air,
        il_risk_penalty=il_norm * w_il,
        tvl_depth_component=tvl_norm * w_tvl,
        volume_consistency_component=vol_con_norm * w_vol,
        rationale=rationale,
    )
