"""engine/strategies/lp_agile/alerts.py — subscriber-facing alert formatting.

Renders polished OPEN/CLOSE/REBAL/HOLD alerts per spec §subscriber-alert-formats.
Two output modes:
  - plain_text  : for Telegram / BMI digest body
  - markdown    : for PWA card / web view (digest_generator.py)

Every OPEN alert carries the dedicated-LP-wallet reminder per
[[feedback-lp-dedicated-wallet]].
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from engine.strategies.lp_agile.types import LPAction, LPSignal


# ---------------------------------------------------------------------------
# Format strings (kept as inline templates so a translator could swap them)
# ---------------------------------------------------------------------------


VERDICT_BADGE = {
    "PASS":        "✅",
    "WATCH":       "⚠",
    "BLOCKED":     "🚫",
    "UNAVAILABLE": "❓",
    "PENDING":     "·",
}

ACTION_EMOJI = {
    LPAction.OPEN:      "💰",
    LPAction.CLOSE:     "🔴",
    LPAction.REBALANCE: "🟡",
    LPAction.HOLD:      "🟢",
}


def render_alert(signal: LPSignal, *, mode: str = "plain_text") -> str:
    """Render a subscriber-facing alert.

    BLOCKED signals get a struck-through header but still ship — transparency-first.
    """
    if signal.action == LPAction.OPEN:
        return _render_open(signal, mode)
    if signal.action == LPAction.CLOSE:
        return _render_close(signal, mode)
    if signal.action == LPAction.REBALANCE:
        return _render_rebalance(signal, mode)
    if signal.action == LPAction.HOLD:
        return _render_hold(signal, mode)
    return f"(unknown action {signal.action})"


# ---------------------------------------------------------------------------
# OPEN — full pre-trade brief
# ---------------------------------------------------------------------------


def _render_open(sig: LPSignal, mode: str) -> str:
    pool = sig.pool
    snap = sig.snapshot_at_signal
    badge = VERDICT_BADGE.get(sig.ai_judge_verdict, "·")
    blocked_strikethrough = sig.ai_judge_verdict == "BLOCKED"

    pool_url = _pool_url(sig)
    fee_apr = float(snap.fee_apr) * 100 if snap and snap.fee_apr else 0.0

    header = (f"💰 LP Opportunity — {pool.protocol.value.upper()} "
              f"{pool.pair} ({pool.fee_tier_bps}bps)")
    if blocked_strikethrough:
        header = f"~~{header}~~  🚫 BLOCKED — see verdict below"

    lines = [
        header,
        "",
        f"Pool:    {pool.pair} on {pool.protocol.value} ({pool.chain.value})",
        f"URL:     {pool_url}",
        "",
        f"Action:  OPEN concentrated liquidity position",
        f"Range:   ${float(sig.range_low_price):,.4f}  →  "
        f"${float(sig.range_high_price):,.4f}   ({sig.range_label})",
        f"Suggest: {float(sig.suggested_capital_pct_of_lp_bankroll)*100:.0f}% of LP bankroll",
        "",
        f"Pool TVL:      ${float(snap.tvl_usd):,.0f}" if snap else "",
        f"24h volume:    ${float(snap.volume_24h_usd):,.0f}" if snap else "",
        f"Fee APR:       {fee_apr:.2f}%  (estimated)",
        f"Yield/day:     ${float(sig.expected_daily_fee_usd_per_1k):,.4f}/day per $1K position",
        "",
        "PRE-OPEN steps:",
        f"  1. Swap to 50/50 split: ~50% {pool.base_symbol} + ~50% {pool.quote_symbol}",
        "  2. Use a DEDICATED LP wallet — NOT your main trading wallet.",
        "     If PRJX/Uniswap/Aerodrome is ever exploited, only your LP capital is at risk.",
        f"  3. Open the position via {pool.protocol.value}'s UI at the URL above",
        f"  4. Confirm range matches: ${float(sig.range_low_price):,.4f} → ${float(sig.range_high_price):,.4f}",
        "",
        "IL projection (vs holding 50/50 unposed):",
    ]
    for move, loss in sig.il_projection.items():
        lines.append(f"  {move:>5}: {loss}")
    lines.extend([
        "",
        "Risk disclosure: Concentrated liquidity is high-risk. IL on volatile",
        "pairs can exceed fee yield. Pool audited: "
        f"{pool.audit_status.replace('_', ' ')}.",
        "",
        f"{badge} AI verdict ({sig.ai_judge_tier}): {sig.ai_judge_verdict}",
        f"  {sig.ai_judge_reasoning}",
    ])
    if sig.rationale:
        lines.extend(["", f"Why now: {sig.rationale}"])

    return "\n".join(line for line in lines if line is not None)


# ---------------------------------------------------------------------------
# CLOSE — exit alert
# ---------------------------------------------------------------------------


def _render_close(sig: LPSignal, mode: str) -> str:
    pool = sig.pool
    badge = VERDICT_BADGE.get(sig.ai_judge_verdict, "·")
    lines = [
        f"🔴 LP Close Alert — {pool.protocol.value.upper()} {pool.pair} ({pool.fee_tier_bps}bps)",
        "",
        f"Your position range: ${float(sig.range_low_price):,.4f} → "
        f"${float(sig.range_high_price):,.4f}",
        "",
        f"Reason: {sig.reason_code or 'see rationale'}",
        f"  {sig.rationale}",
        "",
        "Action: CLOSE position",
        f"  - Remove liquidity via {pool.protocol.value} UI",
        f"  - Receive: your {pool.base_symbol} + {pool.quote_symbol} + accrued fees",
    ]
    if sig.alternative_pool_id:
        lines.extend([
            "",
            f"Next opportunity: rotate to {sig.alternative_pool_id} "
            "(see next OPEN alert in this digest)",
        ])
    lines.extend([
        "",
        f"{badge} AI verdict ({sig.ai_judge_tier}): {sig.ai_judge_verdict}",
        f"  {sig.ai_judge_reasoning}",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# REBALANCE — widen/narrow without closing entirely
# ---------------------------------------------------------------------------


def _render_rebalance(sig: LPSignal, mode: str) -> str:
    pool = sig.pool
    badge = VERDICT_BADGE.get(sig.ai_judge_verdict, "·")
    return "\n".join([
        f"🟡 LP Rebalance Suggestion — {pool.protocol.value.upper()} {pool.pair}",
        "",
        f"Current range: (see open position)",
        f"Suggested new: ${float(sig.range_low_price):,.4f}  →  "
        f"${float(sig.range_high_price):,.4f}   ({sig.range_label})",
        "",
        f"Why: {sig.rationale}",
        "",
        "Action: Close current → open wider range",
        "  Alternative: hold current + accept range-break risk if you're confident in further upside",
        "",
        f"{badge} AI verdict ({sig.ai_judge_tier}): {sig.ai_judge_verdict}",
        f"  {sig.ai_judge_reasoning}",
    ])


# ---------------------------------------------------------------------------
# HOLD — DO-NOTHING (silence is a feature)
# ---------------------------------------------------------------------------


def _render_hold(sig: LPSignal, mode: str) -> str:
    pool = sig.pool
    snap = sig.snapshot_at_signal
    lines = [
        f"🟢 LP position stable — no action needed",
        "",
        f"Pool:   {pool.pair} on {pool.protocol.value}",
    ]
    if snap is not None:
        lines.append(f"Price:  ${float(snap.base_price_usd):,.4f}  (mid-range)")
    lines.extend([
        f"Range:  ${float(sig.range_low_price):,.4f}  →  ${float(sig.range_high_price):,.4f}",
        f"Health: 🟢 GREEN  — earning fees, in range",
        "",
        "Next review in ~6h.",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pool URL helpers
# ---------------------------------------------------------------------------


_POOL_URL_TEMPLATES = {
    "prjx":        "https://www.prjx.com/pool/{addr}",
    "uniswap_v3":  "https://app.uniswap.org/explore/pools/ethereum/{addr}",
    "slipstream":  "https://aerodrome.finance/positions",
    "aerodrome":   "https://aerodrome.finance/positions",
}


def _pool_url(sig: LPSignal) -> str:
    tmpl = _POOL_URL_TEMPLATES.get(sig.pool.protocol.value)
    if not tmpl:
        return ""
    return tmpl.format(addr=sig.pool.pool_address)
