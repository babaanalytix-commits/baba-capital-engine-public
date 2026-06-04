"""engine/strategies/lp_agile/triggers.py — OPEN / CLOSE / REBAL / HOLD decisions.

Inputs:
  ranked_pools : list[RankedPool] from ranker (top-of-book first)
  open_positions : list[LPPosition] the subscriber currently holds (Phase 1
                   best-effort, Phase 2 on-chain verified)
  config : trigger-threshold dict (defaults below)

Outputs: list[LPSignal] — one per actionable item, plus a single HOLD per
healthy position so the digest can include "no action today" reassurance.

Per spec §trigger-logic and §do-nothing-is-a-feature.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from engine.strategies.lp_agile.cost_ledger import realized_il_from_model
from engine.strategies.lp_agile.range_optimizer import compute_range, project_il
from engine.strategies.lp_agile.types import (
    LPAction, LPPosition, LPSignal, Protocol, RankedPool,
)

logger = logging.getLogger("engine.strategies.lp_agile.triggers")

# 2026-05-27: the on-chain executor (sign_and_send_mint) is v0.1 = Slipstream
# only. Generating OPEN signals for protocols we can't mint (prjx/HyperEVM,
# uniswap_v3) just produces alerts that fail at execution — and crowds out the
# mintable pool. So OPEN candidates are restricted to executable protocols.
# Expand this set as PRJX / Uniswap V3 executors land. Env override:
# LP_EXECUTABLE_PROTOCOLS="slipstream,prjx".
import os as _os
_exec_env = _os.environ.get("LP_EXECUTABLE_PROTOCOLS", "slipstream")
EXECUTABLE_PROTOCOLS = {
    p.strip().lower() for p in _exec_env.split(",") if p.strip()
}


# Defaults — Yomi-tunable via env later
DEFAULT_THRESHOLDS = {
    "open_score_advantage_pct": Decimal("0.15"),   # new pool must beat current by 15%
    "close_apr_drop_pct":       Decimal("0.30"),   # close if fee APR dropped 30%
    "close_il_pct":             Decimal("0.10"),   # close on -10% IL
    "rebal_proximity_pct":      Decimal("0.20"),   # rebal hint when within 20% of range edge
    "alternative_score_better_pct": Decimal("0.25"),  # rotate when alt 25% better
}


def evaluate_triggers(
    ranked_pools: list[RankedPool],
    open_positions: Optional[list[LPPosition]] = None,
    *, thresholds: Optional[dict] = None,
    suggested_capital_pct: Decimal = Decimal("0.15"),
) -> list[LPSignal]:
    """Return actionable LPSignals + HOLD signals.

    The caller (scanner) routes OPEN/CLOSE/REBAL through the AI judge before
    delivering to subscribers; HOLD goes straight to the digest's "no action
    today" block.
    """
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    open_positions = open_positions or []
    signals: list[LPSignal] = []

    # Index open positions by pool_id for fast lookup
    open_by_pool_id: dict[str, LPPosition] = {p.pool.id: p for p in open_positions}
    top_ranked = ranked_pools[0] if ranked_pools else None  # true top (for CLOSE-alt compare)

    # ----- OPEN signal: top EXECUTABLE pool, subscriber not in it -----
    # Only suggest opening pools the executor can actually mint (v0.1 =
    # Slipstream). The overall #1 may be prjx/HyperEVM which we can't execute —
    # surfacing it would just fail at mint. Pick the top-ranked executable pool.
    top_executable = next(
        (r for r in ranked_pools
         if r.snapshot.pool.protocol.value.lower() in EXECUTABLE_PROTOCOLS),
        None,
    )
    if top_executable is not None:
        ex_pool_id = top_executable.snapshot.pool.id
        if ex_pool_id not in open_by_pool_id:
            sig = _build_open_signal(top_executable, suggested_capital_pct, th)
            if sig is not None:
                signals.append(sig)
        if top_ranked is not None and top_executable.snapshot.pool.id != top_ranked.snapshot.pool.id:
            logger.info("[triggers] OPEN routed to top EXECUTABLE %s (overall #1 %s "
                        "not mintable by v0.1 executor)",
                        ex_pool_id, top_ranked.snapshot.pool.id)

    # ----- Per-position CLOSE / REBAL / HOLD -----
    for pos in open_positions:
        # Re-rank the pool this position is in (find its snapshot)
        current_ranked = next(
            (r for r in ranked_pools if r.snapshot.pool.id == pos.pool.id),
            None,
        )
        if current_ranked is None:
            # Position's pool dropped from universe (delisted? security event?)
            signals.append(_build_close_signal(
                pos, reason="pool_dropped_from_universe",
                alt_pool=top_ranked, th=th,
            ))
            continue

        # CLOSE trigger 1: better alternative
        if top_ranked is not None and top_ranked.snapshot.pool.id != pos.pool.id:
            adv = (top_ranked.score - current_ranked.score)
            if (current_ranked.score > 0
                    and adv / current_ranked.score >= th["alternative_score_better_pct"]):
                signals.append(_build_close_signal(
                    pos, reason="alternative_pool_materially_better",
                    alt_pool=top_ranked, th=th,
                ))
                continue

        # CLOSE trigger 2: price exited range
        snap = current_ranked.snapshot
        in_range = pos.range_low_price <= snap.base_price_usd <= pos.range_high_price
        if not in_range:
            signals.append(_build_close_signal(
                pos, reason="price_exited_range",
                alt_pool=None, th=th,
            ))
            continue

        # CLOSE trigger 3: impermanent loss exceeds threshold (LP-2 — wires the
        # previously-dead close_il_pct rule). For a position still IN range,
        # compute current unrealised concentrated IL vs the entry basket (entry
        # price ≈ geometric centre of the range, which compute_range builds
        # around the entry price). Close if IL ≥ close_il_pct.
        try:
            entry_proxy = (pos.range_low_price * pos.range_high_price).sqrt()
            ilr = realized_il_from_model(
                entry_price=entry_proxy, exit_price=snap.base_price_usd,
                deposited_usd=Decimal("1000"),   # nominal — cancels in the ratio
                range_low=pos.range_low_price, range_high=pos.range_high_price,
            )
            hodl = ilr["hodl_value_usd"]
            il_frac = (ilr["il_usd"] / hodl) if hodl > 0 else Decimal(0)
            if il_frac >= th["close_il_pct"]:
                logger.info("[triggers] IL close %s: il=%.1f%% >= %.0f%% threshold",
                            pos.position_id, float(il_frac) * 100,
                            float(th["close_il_pct"]) * 100)
                signals.append(_build_close_signal(
                    pos, reason="il_exceeded_threshold", alt_pool=top_ranked, th=th,
                ))
                continue
        except Exception as exc:                              # noqa: BLE001
            logger.debug("[triggers] IL check skipped for %s: %s", pos.position_id, exc)

        # REBAL trigger: price approaching edge
        range_width = pos.range_high_price - pos.range_low_price
        dist_to_upper = pos.range_high_price - snap.base_price_usd
        dist_to_lower = snap.base_price_usd - pos.range_low_price
        if range_width > 0:
            proximity = min(dist_to_upper, dist_to_lower) / range_width
            if proximity < th["rebal_proximity_pct"]:
                signals.append(_build_rebalance_signal(pos, current_ranked, th))
                continue

        # HOLD — explicit "no action" so the digest can echo it
        signals.append(_build_hold_signal(pos, current_ranked))

    return signals


# ---------------------------------------------------------------------------
# Signal builders
# ---------------------------------------------------------------------------


def _build_open_signal(
    rank: RankedPool, suggested_capital_pct: Decimal, th: dict,
) -> Optional[LPSignal]:
    snap = rank.snapshot
    pool = snap.pool
    range_choice = compute_range(snap)
    if range_choice is None:
        return None
    range_low, range_high, _, _ = range_choice.chosen("balanced")

    # Daily fee yield per $1K position
    if snap.tvl_usd > 0 and snap.fees_7d_usd > 0:
        # subscriber's $1000 captures (1000/TVL) of the 7d fees, then per day
        daily_per_1k = (Decimal(1000) / snap.tvl_usd) * snap.fees_7d_usd / Decimal(7)
    else:
        daily_per_1k = Decimal(0)

    # IL projection at a few common price moves
    il_proj = {
        "+20%": f"{float(project_il(Decimal('0.20')))*100:+.2f}%",
        "-20%": f"{float(project_il(Decimal('-0.20')))*100:+.2f}%",
        "-50%": f"{float(project_il(Decimal('-0.50')))*100:+.2f}% (likely range-break)",
    }

    rationale = (
        f"Top-ranked pool right now. {rank.rationale}. "
        f"Range covers ±{range_choice.sigma_used*200:.0f}% of typical 7-day price swings."
    )

    return LPSignal(
        strategy_id="lp_agile_subscriber_v1",
        generated_at_iso=datetime.now(timezone.utc).isoformat(),
        signal_id=f"lp-{pool.id}-{uuid.uuid4().hex[:8]}",
        action=LPAction.OPEN,
        pool=pool,
        snapshot_at_signal=snap,
        range_low_price=range_low,
        range_high_price=range_high,
        range_label=f"balanced ±2σ ({range_choice.sigma_used*200:.0f}%)",
        suggested_capital_pct_of_lp_bankroll=suggested_capital_pct,
        expected_daily_fee_usd_per_1k=daily_per_1k,
        expected_airdrop_pts_per_day=snap.airdrop_points_per_usd_day,
        il_projection=il_proj,
        rationale=rationale,
        ai_judge_verdict="PENDING",
        ai_judge_tier="tier1_rule",
        ai_judge_reasoning="Tier 2 AI judge wires Day 4 — currently rule-pass only",
        metadata={"score": str(rank.score)},
    )


def _build_close_signal(
    pos: LPPosition, *, reason: str, alt_pool: Optional[RankedPool], th: dict,
) -> LPSignal:
    rationale_map = {
        "price_exited_range": (
            f"Price ${pos.range_low_price:.4f}–${pos.range_high_price:.4f} range broken — "
            "position no longer earns fees. Exit + redeploy to active range."
        ),
        "alternative_pool_materially_better": (
            f"Better pool now available "
            f"({alt_pool.snapshot.pool.id if alt_pool else '?'}). "
            "Rotation captures the higher APR + lower IL profile."
        ),
        "pool_dropped_from_universe": (
            "Pool removed from active universe (TVL drop / security event / audit "
            "revoked). Close immediately to avoid further exposure."
        ),
        "il_exceeded_threshold": (
            f"Impermanent loss on this position has exceeded the "
            f"{float(th['close_il_pct'])*100:.0f}% close threshold — the divergence "
            "drag now outweighs fee accrual. Exit to stop the bleed + redeploy."
        ),
    }
    rationale = rationale_map.get(reason, f"Close due to: {reason}")
    return LPSignal(
        strategy_id="lp_agile_subscriber_v1",
        generated_at_iso=datetime.now(timezone.utc).isoformat(),
        signal_id=f"lp-close-{pos.position_id}-{uuid.uuid4().hex[:6]}",
        action=LPAction.CLOSE,
        pool=pos.pool,
        snapshot_at_signal=pos.pool if False else None,  # filled by scanner with fresh snap
        range_low_price=pos.range_low_price,
        range_high_price=pos.range_high_price,
        range_label="(existing position)",
        suggested_capital_pct_of_lp_bankroll=Decimal(0),
        expected_daily_fee_usd_per_1k=Decimal(0),
        expected_airdrop_pts_per_day=None,
        il_projection={},
        rationale=rationale,
        ai_judge_verdict="PENDING",
        ai_judge_tier="tier1_rule",
        ai_judge_reasoning="Close-trigger CLOSE; AI judge confirms reason validity Day 4.",
        referenced_position_id=pos.position_id,
        reason_code=reason,
        alternative_pool_id=alt_pool.snapshot.pool.id if alt_pool else None,
    )


def _build_rebalance_signal(
    pos: LPPosition, current: RankedPool, th: dict,
) -> LPSignal:
    snap = current.snapshot
    new_range = compute_range(snap)
    if new_range is None:
        # Fall back to HOLD
        return _build_hold_signal(pos, current)
    new_low, new_high, _, _ = new_range.chosen("balanced")
    rationale = (
        f"Price ${snap.base_price_usd:.4f} approaching range edge "
        f"[${pos.range_low_price:.4f}, ${pos.range_high_price:.4f}]. "
        f"Suggest widening to [${new_low:.4f}, ${new_high:.4f}] before break-out."
    )
    return LPSignal(
        strategy_id="lp_agile_subscriber_v1",
        generated_at_iso=datetime.now(timezone.utc).isoformat(),
        signal_id=f"lp-rebal-{pos.position_id}-{uuid.uuid4().hex[:6]}",
        action=LPAction.REBALANCE,
        pool=pos.pool,
        snapshot_at_signal=snap,
        range_low_price=new_low,
        range_high_price=new_high,
        range_label=f"new balanced ±{new_range.sigma_used*200:.0f}%",
        suggested_capital_pct_of_lp_bankroll=Decimal(0),
        expected_daily_fee_usd_per_1k=Decimal(0),
        expected_airdrop_pts_per_day=None,
        il_projection={},
        rationale=rationale,
        ai_judge_verdict="PENDING",
        ai_judge_tier="tier1_rule",
        ai_judge_reasoning="Range-edge proximity. Tier 2 confirms market regime Day 4.",
        referenced_position_id=pos.position_id,
        reason_code="range_proximity",
    )


def _build_hold_signal(pos: LPPosition, current: RankedPool) -> LPSignal:
    snap = current.snapshot
    rationale = (
        f"Position healthy. Price ${snap.base_price_usd:.4f} mid-range. "
        f"Position earning at current ranking. No action today."
    )
    return LPSignal(
        strategy_id="lp_agile_subscriber_v1",
        generated_at_iso=datetime.now(timezone.utc).isoformat(),
        signal_id=f"lp-hold-{pos.position_id}-{uuid.uuid4().hex[:6]}",
        action=LPAction.HOLD,
        pool=pos.pool,
        snapshot_at_signal=snap,
        range_low_price=pos.range_low_price,
        range_high_price=pos.range_high_price,
        range_label="(existing position)",
        suggested_capital_pct_of_lp_bankroll=Decimal(0),
        expected_daily_fee_usd_per_1k=Decimal(0),
        expected_airdrop_pts_per_day=None,
        il_projection={},
        rationale=rationale,
        ai_judge_verdict="PASS",
        ai_judge_tier="tier1_rule",
        ai_judge_reasoning="HOLD is the default safe outcome.",
        referenced_position_id=pos.position_id,
        hold_notes="silence is a feature — DO-NOTHING delivered to digest as reassurance",
    )
