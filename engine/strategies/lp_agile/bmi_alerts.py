"""engine/strategies/lp_agile/bmi_alerts.py — BMI Premium subscriber LP alerts.

Per [[feedback-premium-ui-standard]] standing directive: subscriber-facing
LP setup cards must feel PREMIUM. Used by BMI digest + Telegram push.

Two render targets:
  * render_telegram_html() — uses Telegram <b>/<i>/<u>/<code>/<a> tags (no markdown leakage).
  * render_email_card_html()  — clean HTML <div> card for digest emails / PWA.

Subscriber tier handling:
  * BMI Premium     → OPEN + CLOSE setups (daily digest + immediate push on CLOSE)
  * BMI Premium+    → adds REBAL suggestions + AI hedge thesis + earlier delivery

All alerts:
  * Lead with the verdict + action emoji
  * Hero values up front (APR estimate, range, suggested capital)
  * Concrete pre-conditions ("swap $X USDC → cbBTC first")
  * Dedicated-LP-wallet reminder on every OPEN
  * One CTA: deep link to the protocol's UI
  * AI verdict badge at the bottom (PASS / WATCH / BLOCKED)
"""
from __future__ import annotations

from decimal import Decimal
from html import escape
from typing import Optional

from engine.strategies.lp_agile.alerts import _pool_url
from engine.strategies.lp_agile.types import LPAction, LPSignal


# Tier-specific copy
TIER_UPGRADE_NOTE = (
    "<i>Premium+ subscribers receive rebalance alerts and AI hedge "
    "suggestions on this position. <a href=\"https://gumroad.com/baba\">"
    "Upgrade to Premium+</a>.</i>"
)

VENUE_LABEL = {
    "prjx":        "PRJX (HyperEVM)",
    "uniswap_v3":  "Uniswap V3 (Ethereum)",
    "slipstream":  "Aerodrome Slipstream (Base)",
    "aerodrome":   "Aerodrome (Base)",
}

ACTION_EMOJI = {
    LPAction.OPEN:      "💰",
    LPAction.CLOSE:     "🔴",
    LPAction.REBALANCE: "🟡",
    LPAction.HOLD:      "🟢",
}

VERDICT_BADGE_HTML = {
    "PASS":    "<b>✅ PASS</b>",
    "WATCH":   "<b>⚠ WATCH</b>",
    "BLOCKED": "<b>🚫 BLOCKED</b>",
    "PENDING": "<i>· pending review</i>",
}


# ---------------------------------------------------------------------------
# Telegram HTML
# ---------------------------------------------------------------------------


def render_telegram_html(signal: LPSignal, *, tier: str = "premium") -> str:
    """Polished BMI-Premium-grade Telegram alert for one LP signal.

    `tier`: "premium" (Premium only) or "premium_plus" (extra context).
    Returns Telegram-HTML-formatted string (Telegram supports <b>/<i>/<u>/<code>/<a>).
    """
    if signal.action == LPAction.OPEN:
        return _render_open_tg(signal, tier)
    if signal.action == LPAction.CLOSE:
        return _render_close_tg(signal, tier)
    if signal.action == LPAction.REBALANCE:
        return _render_rebal_tg(signal, tier)
    if signal.action == LPAction.HOLD:
        return _render_hold_tg(signal, tier)
    return ""


def _render_open_tg(sig: LPSignal, tier: str) -> str:
    pool = sig.pool
    snap = sig.snapshot_at_signal
    venue_label = VENUE_LABEL.get(pool.protocol.value, pool.protocol.value.title())
    fee_apr = float(snap.fee_apr) * 100 if snap and snap.fee_apr else 0.0
    # 2026-05-25 audit-pickup autoship: APR sanity guard. Subscriber-facing
    # LP card MUST NOT render numbers we can't defend. 2026-05-25 12:00 UTC
    # us-session digest carried trading_fees=5673.24% for cbBTC/USDC — almost
    # certainly an upstream unit-drift regression (see memory
    # [[feedback_lp_apr_unit_drift]]). Block the card and surface to admin
    # rather than publish a number that breaks subscriber trust. Threshold:
    # 500% APR — anything legitimately above that is a once-in-a-cycle event
    # worth manual review anyway.
    if fee_apr > 500.0:
        try:
            import logging
            logging.getLogger("engine.strategies.lp_agile.bmi_alerts").error(
                "APR sanity guard tripped on %s — fee_apr=%.2f%% (raw=%s). "
                "Suppressing subscriber card.",
                pool.id, fee_apr, snap.fee_apr,
            )
        except Exception:  # noqa: BLE001
            pass
        return ""  # caller stacks multiple cards; an empty one is silently skipped
    badge = VERDICT_BADGE_HTML.get(sig.ai_judge_verdict, "<i>· pending</i>")

    # Hero block
    lines = [
        f"{ACTION_EMOJI[LPAction.OPEN]} <b>LP Setup — {escape(pool.pair)}</b>",
        f"<i>{escape(venue_label)}  ·  {pool.fee_tier_bps}bps fee tier</i>",
        "",
        f"📊 <b>Hero numbers</b>",
        f"  Fee APR (est)     : <b>{fee_apr:.1f}%</b>",
        f"  Pool TVL          : ${float(snap.tvl_usd):,.0f}",
        f"  24h volume        : ${float(snap.volume_24h_usd):,.0f}",
        "",
        f"🎯 <b>Setup</b>",
        f"  Action            : OPEN concentrated liquidity",
        f"  Range             : ${float(sig.range_low_price):,.2f}  →  ${float(sig.range_high_price):,.2f}",
        f"  Range type        : <i>{escape(sig.range_label)}</i>",
        f"  Suggested capital : {float(sig.suggested_capital_pct_of_lp_bankroll)*100:.0f}% of LP bankroll",
        f"  Daily yield est   : ${float(sig.expected_daily_fee_usd_per_1k):.4f}/day per $1,000",
        "",
        f"⚠ <b>IL projection</b>",
    ]
    for move, loss in (sig.il_projection or {}).items():
        lines.append(f"  {escape(str(move)):>5s} price move : {escape(str(loss))}")
    lines.extend([
        "",
        f"📝 <b>How to execute</b> (you execute on your own wallet — we never custody)",
        f"  1. Use a <u>dedicated LP wallet</u>, not your main trading wallet",
        f"  2. Swap to 50/50 split: ~50% {escape(pool.base_symbol)} + ~50% {escape(pool.quote_symbol)}",
        f"  3. Open at <a href=\"{escape(_pool_url(sig))}\">{escape(venue_label)}</a>",
        f"  4. Confirm range matches above",
        "",
        f"{badge}  <i>{escape(sig.ai_judge_reasoning or '')}</i>",
    ])

    if tier == "premium_plus":
        lines.extend([
            "",
            f"💎 <b>Premium+ — AI thesis</b>",
            f"  <i>{escape((sig.rationale or '')[:280])}</i>",
        ])
    else:
        lines.extend([
            "",
            TIER_UPGRADE_NOTE,
        ])

    lines.append(f"\n<code>id: {sig.signal_id}</code>")
    return "\n".join(lines)


def _render_close_tg(sig: LPSignal, tier: str) -> str:
    pool = sig.pool
    venue_label = VENUE_LABEL.get(pool.protocol.value, pool.protocol.value.title())
    badge = VERDICT_BADGE_HTML.get(sig.ai_judge_verdict, "<i>· pending</i>")
    reason_map = {
        "price_exited_range": "Price moved outside your range — position no longer earning fees",
        "alternative_pool_materially_better": "A better pool now ranks higher — rotation captures more APR",
        "pool_dropped_from_universe": "Pool removed from active universe — exit immediately",
        "range_proximity": "Price approaching range edge — close + reopen wider",
    }
    reason_plain = reason_map.get(sig.reason_code or "", "Conditions changed — see thesis below")
    lines = [
        f"{ACTION_EMOJI[LPAction.CLOSE]} <b>LP Close Alert — {escape(pool.pair)}</b>",
        f"<i>{escape(venue_label)}</i>",
        "",
        f"🎯 <b>Action: CLOSE this position now</b>",
        f"  Why: {escape(reason_plain)}",
        "",
        f"📝 <b>Steps</b>",
        f"  1. Open <a href=\"{escape(_pool_url(sig))}\">{escape(venue_label)}</a>",
        f"  2. Find your {escape(pool.pair)} position and remove all liquidity",
        f"  3. Collect any accrued fees",
        f"  4. Wait for the next OPEN alert (or swap proceeds back to USDC)",
        "",
        f"{badge}  <i>{escape(sig.ai_judge_reasoning or '')}</i>",
    ]
    if sig.alternative_pool_id:
        lines.extend([
            "",
            f"➡ <b>Next opportunity</b>: <code>{escape(sig.alternative_pool_id)}</code> — alert on the way.",
        ])
    lines.append(f"\n<code>id: {sig.signal_id}</code>")
    return "\n".join(lines)


def _render_rebal_tg(sig: LPSignal, tier: str) -> str:
    if tier != "premium_plus":
        # Rebalance suggestions are Premium+ only
        return ""
    pool = sig.pool
    venue_label = VENUE_LABEL.get(pool.protocol.value, pool.protocol.value.title())
    badge = VERDICT_BADGE_HTML.get(sig.ai_judge_verdict, "<i>· pending</i>")
    return "\n".join([
        f"{ACTION_EMOJI[LPAction.REBALANCE]} <b>LP Rebalance — {escape(pool.pair)}</b>",
        f"<i>{escape(venue_label)}  ·  💎 Premium+ alert</i>",
        "",
        f"📊 <b>Suggested new range</b>",
        f"  ${float(sig.range_low_price):,.2f}  →  ${float(sig.range_high_price):,.2f}",
        f"  <i>{escape(sig.range_label)}</i>",
        "",
        f"🎯 <b>Action: Close current → open at new range</b>",
        f"  Alternative: hold current + accept range-break risk",
        "",
        f"📝 <i>Why: {escape(sig.rationale or '')}</i>",
        "",
        f"  Execute at: <a href=\"{escape(_pool_url(sig))}\">{escape(venue_label)}</a>",
        "",
        f"{badge}  <i>{escape(sig.ai_judge_reasoning or '')}</i>",
        f"\n<code>id: {sig.signal_id}</code>",
    ])


def _render_hold_tg(sig: LPSignal, tier: str) -> str:
    pool = sig.pool
    snap = sig.snapshot_at_signal
    price_now = float(snap.base_price_usd) if snap else 0
    return "\n".join([
        f"{ACTION_EMOJI[LPAction.HOLD]} <b>LP position healthy — no action</b>",
        f"<i>{escape(pool.pair)}  ·  {VENUE_LABEL.get(pool.protocol.value, pool.protocol.value)}</i>",
        "",
        f"  Current price: ${price_now:,.2f} (mid-range)",
        f"  Your range  : ${float(sig.range_low_price):,.2f}  →  ${float(sig.range_high_price):,.2f}",
        f"  Health      : 🟢 GREEN — in range, earning fees",
        "",
        f"  Next review in ~6h.",
    ])


# ---------------------------------------------------------------------------
# Email / digest HTML card
# ---------------------------------------------------------------------------


VERDICT_BORDER_COLOR = {
    "PASS":    "#22c55e",   # green
    "WATCH":   "#f59e0b",   # amber
    "BLOCKED": "#ef4444",   # red
    "PENDING": "#6b7280",   # gray
}


def render_email_card_html(signal: LPSignal, *, tier: str = "premium") -> str:
    """Polished HTML card for inclusion in BMI Premium daily digest email.

    Returns a single `<div>` element with inline styling (email-safe).
    Caller wraps multiple cards in a stack for the digest body.
    """
    if signal.action not in (LPAction.OPEN, LPAction.CLOSE, LPAction.REBALANCE, LPAction.HOLD):
        return ""
    pool = signal.pool
    snap = signal.snapshot_at_signal
    venue_label = VENUE_LABEL.get(pool.protocol.value, pool.protocol.value.title())
    verdict = signal.ai_judge_verdict or "PENDING"
    border = VERDICT_BORDER_COLOR.get(verdict, "#6b7280")
    emoji = ACTION_EMOJI.get(signal.action, "·")
    action_label = signal.action.value.upper()
    fee_apr = float(snap.fee_apr) * 100 if snap and snap.fee_apr else 0.0
    pool_url = _pool_url(signal)
    upgrade_html = (
        ""
        if tier == "premium_plus"
        else "<p style=\"margin-top:12px;font-size:11px;color:#94a3b8;\">"
             "Premium+ subscribers receive rebalance alerts and AI hedge suggestions. "
             "<a href=\"https://gumroad.com/baba\" style=\"color:#3b82f6;\">Upgrade</a>.</p>"
    )

    # Hero line
    hero = f"{fee_apr:.1f}% APR" if signal.action == LPAction.OPEN else action_label
    if signal.action == LPAction.OPEN:
        body_html = _open_body_html(signal, snap, pool, venue_label, pool_url)
    elif signal.action == LPAction.CLOSE:
        body_html = _close_body_html(signal, pool, venue_label, pool_url)
    elif signal.action == LPAction.REBALANCE:
        body_html = _rebal_body_html(signal, pool, venue_label, pool_url) if tier == "premium_plus" else ""
        if not body_html:
            return ""
    else:
        body_html = _hold_body_html(signal, snap, pool)

    return f"""
<div style="border-left:4px solid {border};background:#0f172a;color:#e2e8f0;
            padding:16px 18px;margin:10px 0;border-radius:6px;font-family:-apple-system,BlinkMacSystemFont,sans-serif;">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;">
    <div>
      <div style="font-size:12px;color:#94a3b8;letter-spacing:0.5px;text-transform:uppercase;">
        {emoji} LP · {escape(pool.protocol.value.upper())}
      </div>
      <div style="font-size:18px;font-weight:600;color:#f1f5f9;margin-top:2px;">
        {escape(pool.pair)} <span style="color:#94a3b8;font-weight:400;font-size:13px;">· {pool.fee_tier_bps}bps</span>
      </div>
    </div>
    <div style="text-align:right;">
      <div style="font-size:20px;font-weight:700;color:{border};">{escape(hero)}</div>
      <div style="font-size:11px;color:#94a3b8;margin-top:2px;">{escape(verdict)} · {escape(signal.ai_judge_tier or '')}</div>
    </div>
  </div>
  {body_html}
  <p style="margin-top:10px;font-size:11px;color:#64748b;font-style:italic;">
    AI: {escape((signal.ai_judge_reasoning or '')[:200])}
  </p>
  {upgrade_html}
</div>
""".strip()


def _open_body_html(sig, snap, pool, venue_label, pool_url) -> str:
    tvl = float(snap.tvl_usd) if snap else 0
    vol = float(snap.volume_24h_usd) if snap else 0
    il = sig.il_projection or {}
    il_rows = "".join(
        f"<tr><td style='color:#94a3b8;padding:2px 8px;'>{escape(str(m))}</td>"
        f"<td style='padding:2px 0;color:#f1f5f9;'>{escape(str(v))}</td></tr>"
        for m, v in il.items()
    )
    return f"""
<div style="font-size:13px;line-height:1.5;color:#cbd5e1;">
  <div style="margin-bottom:8px;">
    <span style="color:#94a3b8;">Range:</span>
    <b style="color:#f1f5f9;">${float(sig.range_low_price):,.2f} → ${float(sig.range_high_price):,.2f}</b>
    <span style="color:#94a3b8;font-size:11px;"> ({escape(sig.range_label)})</span>
  </div>
  <div style="margin-bottom:8px;">
    <span style="color:#94a3b8;">Suggested:</span>
    <b style="color:#f1f5f9;">{float(sig.suggested_capital_pct_of_lp_bankroll)*100:.0f}% of LP bankroll</b>
    · <span style="color:#94a3b8;">~${float(sig.expected_daily_fee_usd_per_1k):.4f}/day per $1K</span>
  </div>
  <div style="margin-bottom:8px;font-size:12px;color:#94a3b8;">
    Pool TVL <b style="color:#cbd5e1;">${tvl:,.0f}</b>
    · 24h vol <b style="color:#cbd5e1;">${vol:,.0f}</b>
  </div>
  <details style="margin:8px 0;">
    <summary style="cursor:pointer;color:#94a3b8;font-size:12px;">📝 How to execute</summary>
    <ol style="margin:8px 0;padding-left:20px;font-size:12px;color:#cbd5e1;line-height:1.6;">
      <li>Use a <u>dedicated LP wallet</u>, not your main trading wallet.</li>
      <li>Swap to 50/50: ~50% {escape(pool.base_symbol)} + ~50% {escape(pool.quote_symbol)}</li>
      <li>Open at <a href="{escape(pool_url)}" style="color:#3b82f6;">{escape(venue_label)}</a></li>
      <li>Confirm the range matches above</li>
    </ol>
  </details>
  <details style="margin:8px 0;">
    <summary style="cursor:pointer;color:#94a3b8;font-size:12px;">⚠ IL projection</summary>
    <table style="border-collapse:collapse;font-size:12px;margin-top:6px;">{il_rows}</table>
  </details>
</div>
""".strip()


def _close_body_html(sig, pool, venue_label, pool_url) -> str:
    reason_map = {
        "price_exited_range": "Price moved outside your range — position no longer earning fees.",
        "alternative_pool_materially_better": "A better pool now ranks higher — rotation captures more APR.",
        "pool_dropped_from_universe": "Pool removed from active universe — exit immediately.",
        "range_proximity": "Price approaching range edge — close + reopen wider.",
    }
    reason = reason_map.get(sig.reason_code or "", "Conditions changed — see AI verdict below.")
    return f"""
<div style="font-size:13px;line-height:1.5;color:#cbd5e1;">
  <div style="margin-bottom:8px;color:#fbbf24;"><b>🎯 Close this position now.</b></div>
  <div style="margin-bottom:8px;color:#cbd5e1;font-size:13px;">{escape(reason)}</div>
  <ol style="margin:8px 0;padding-left:20px;font-size:12px;color:#cbd5e1;line-height:1.6;">
    <li>Open <a href="{escape(pool_url)}" style="color:#3b82f6;">{escape(venue_label)}</a></li>
    <li>Find your {escape(pool.pair)} position and remove liquidity</li>
    <li>Collect accrued fees</li>
    <li>Wait for the next OPEN alert (or swap to USDC)</li>
  </ol>
</div>
""".strip()


def _rebal_body_html(sig, pool, venue_label, pool_url) -> str:
    return f"""
<div style="font-size:13px;line-height:1.5;color:#cbd5e1;">
  <div style="margin-bottom:8px;color:#fbbf24;">💎 <b>Premium+ rebalance alert</b></div>
  <div style="margin-bottom:8px;">
    <span style="color:#94a3b8;">Suggested new range:</span>
    <b style="color:#f1f5f9;">${float(sig.range_low_price):,.2f} → ${float(sig.range_high_price):,.2f}</b>
  </div>
  <div style="margin-bottom:8px;color:#cbd5e1;font-size:12px;font-style:italic;">{escape(sig.rationale or '')}</div>
  <div style="margin-top:8px;color:#94a3b8;font-size:12px;">
    Action: close + reopen at new range — or hold and accept range-break risk.
    Execute at <a href="{escape(pool_url)}" style="color:#3b82f6;">{escape(venue_label)}</a>.
  </div>
</div>
""".strip()


def _hold_body_html(sig, snap, pool) -> str:
    price_now = float(snap.base_price_usd) if snap else 0
    return f"""
<div style="font-size:13px;line-height:1.5;color:#cbd5e1;">
  <div style="color:#94a3b8;">
    Price now: <b style="color:#f1f5f9;">${price_now:,.2f}</b>
    · Your range: ${float(sig.range_low_price):,.2f} → ${float(sig.range_high_price):,.2f}
  </div>
  <div style="margin-top:6px;color:#22c55e;font-size:12px;">
    🟢 In range, earning fees. Next review ~6h.
  </div>
</div>
""".strip()


# ---------------------------------------------------------------------------
# Convenience: build a multi-signal digest block for the BMI Premium email
# ---------------------------------------------------------------------------


def render_digest_section_html(
    signals: list[LPSignal], *, tier: str = "premium",
) -> str:
    """Return a complete '🌊 LP Setups' section HTML block for the BMI digest.

    Empty string if no signals to surface (silence-is-a-feature for the digest).
    Filters HOLD signals out of the digest — those go in a separate "open positions
    health" block.
    """
    actionable = [s for s in signals if s.action != LPAction.HOLD]
    if not actionable:
        return ""

    cards = "".join(render_email_card_html(s, tier=tier) for s in actionable)
    if not cards:
        return ""

    return f"""
<div style="margin:20px 0;padding:0;">
  <div style="font-size:14px;font-weight:600;color:#f1f5f9;text-transform:uppercase;
              letter-spacing:1px;border-bottom:1px solid #1e293b;padding-bottom:6px;margin-bottom:8px;">
    🌊 LP Setups
    <span style="color:#94a3b8;font-weight:400;text-transform:none;font-size:12px;
                 letter-spacing:0;float:right;">
      {len(actionable)} active · BMI {escape(tier.replace('_', ' ').title())}
    </span>
  </div>
  {cards}
</div>
""".strip()
