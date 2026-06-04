"""engine/strategies/lp_agile/telegram_notify.py — Phase D2 (#373).

Sends LP rebalance plans to operator Telegram with inline Approve/Reject
buttons. The buttons' callback_data carries everything the worker needs to
create a valid approval_store entry — no separate draft-state file.

callback_data format (each ≤64 bytes per Telegram's limit):

   lp_approve:<p|a>:<tokenId>:<tick_lower>:<tick_upper>
   lp_reject:<p|a>:<tokenId>

Examples:
   lp_approve:p:476237:-203700:-194300  (prjx, WHYPE/UBTC)
   lp_approve:a:71481609:-10000:-9000   (aerodrome, cbBTC/USDC)

The pillar is compressed to 1 char (p=prjx, a=aerodrome) to leave room for
larger tokenIds + signed int24 ticks.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.telegram_notify")

# Used by the worker too — keep these constants in sync if you change them.
PILLAR_CODES = {"prjx": "p", "aerodrome": "a"}
PILLAR_BY_CODE = {v: k for k, v in PILLAR_CODES.items()}


def _build_callback_data(
    action: str,
    pillar: str,
    token_id: int,
    tick_lower: Optional[int] = None,
    tick_upper: Optional[int] = None,
) -> str:
    code = PILLAR_CODES.get(pillar)
    if code is None:
        raise ValueError(f"unknown pillar: {pillar}")
    if action == "lp_approve":
        cd = f"lp_approve:{code}:{int(token_id)}:{int(tick_lower)}:{int(tick_upper)}"
    elif action == "lp_reject":
        cd = f"lp_reject:{code}:{int(token_id)}"
    else:
        raise ValueError(f"unknown action: {action}")
    if len(cd.encode("utf-8")) > 64:
        raise ValueError(f"callback_data exceeds 64 bytes: {cd!r}")
    return cd


def format_plan_message(plan: dict) -> str:
    """Build the operator-facing message body."""
    tid = plan.get("nft_token_id") or plan.get("tokenId")
    pair_label = plan.get("pair_label") or plan.get("pool_label") or "?/?"
    proto = plan.get("protocol") or plan.get("pillar") or "?"
    cur_lo = plan.get("current_price_lower") or plan.get("current_range_low")
    cur_hi = plan.get("current_price_upper") or plan.get("current_range_high")
    new_lo_px = plan.get("new_price_lower")
    new_hi_px = plan.get("new_price_upper")
    cur_px = plan.get("pool_price_now") or plan.get("current_price")
    rationale = plan.get("rationale") or plan.get("trigger") or "drift trigger"
    value_usd = plan.get("position_value_usd") or plan.get("value_usd")
    gas_est = plan.get("estimated_gas_usd") or plan.get("gas_usd_est")

    lines = [
        f"<b>🔄 LP rebalance proposed</b>",
        f"<b>{pair_label}</b> ({proto}) — tokenId {tid}",
        "",
    ]
    if value_usd is not None:
        lines.append(f"position value: ${float(value_usd):.2f}")
    if cur_px is not None:
        lines.append(f"current price: {cur_px:g}")
    if cur_lo is not None and cur_hi is not None:
        lines.append(f"current range: {cur_lo:g} → {cur_hi:g}")
    if new_lo_px is not None and new_hi_px is not None:
        lines.append(f"<b>new range: {new_lo_px:g} → {new_hi_px:g}</b>")
    if gas_est is not None:
        lines.append(f"gas estimate: ~${float(gas_est):.2f}")
    lines += ["", f"<i>{rationale}</i>", "",
              "Tap <b>Approve</b> to execute. Window: 15 min."]
    return "\n".join(lines)


def send_rebalance_approval_request(plan: dict) -> Optional[int]:
    """Emit operator Telegram message + Approve/Reject buttons for a plan.

    Returns the Telegram message_id, or None on failure. Caller may want to
    persist the message_id for later edit-on-execute, but it's optional.
    """
    tid = plan.get("nft_token_id") or plan.get("tokenId")
    proto = (plan.get("protocol") or plan.get("pillar") or "").lower()
    pillar = None
    if "prjx" in proto or "hyperevm" in proto:
        pillar = "prjx"
    elif "aerodrome" in proto or "slipstream" in proto or "base" in proto:
        pillar = "aerodrome"
    if pillar is None or tid is None:
        logger.warning(
            "[lp_notify] cannot send — missing pillar/tokenId: %s", plan)
        return None

    new_lo = plan.get("new_tick_lower")
    new_hi = plan.get("new_tick_upper")
    if new_lo is None or new_hi is None:
        logger.info(
            "[lp_notify] skipping plan for tokenId=%s — no executable ticks "
            "yet (planner hasn't decided)", tid)
        return None

    try:
        approve_cd = _build_callback_data(
            "lp_approve", pillar, int(tid), int(new_lo), int(new_hi))
        reject_cd = _build_callback_data(
            "lp_reject", pillar, int(tid))
    except ValueError as e:
        logger.error("[lp_notify] callback_data build failed: %s", e)
        return None

    keyboard = [[
        {"text": "✅ Approve", "callback_data": approve_cd},
        {"text": "❌ Reject", "callback_data": reject_cd},
    ]]
    text = format_plan_message(plan)

    try:
        from ops.bmi.telegram_poster import (
            post_with_inline_keyboard, Tier,
        )
        return post_with_inline_keyboard(
            text=text,
            inline_keyboard=keyboard,
            tier=Tier.ADMIN,
            parse_mode="HTML",
        )
    except Exception as e:                                       # noqa: BLE001
        logger.error("[lp_notify] post_with_inline_keyboard failed: %s", e)
        return None


# ─── Helper for the worker callback handler ────────────────────────────────


def parse_lp_callback(data: str) -> Optional[dict]:
    """Parse a `lp_approve:` / `lp_reject:` callback_data string.

    Returns:
        None if not a valid LP callback.
        dict {"action", "pillar", "token_id", "tick_lower", "tick_upper"}
        on success. tick_lower/tick_upper are None for reject.
    """
    parts = data.split(":")
    if len(parts) < 3:
        return None
    action = parts[0]
    if action not in ("lp_approve", "lp_reject"):
        return None
    code = parts[1]
    pillar = PILLAR_BY_CODE.get(code)
    if pillar is None:
        return None
    try:
        token_id = int(parts[2])
    except ValueError:
        return None
    if action == "lp_approve":
        if len(parts) != 5:
            return None
        try:
            tick_lower = int(parts[3])
            tick_upper = int(parts[4])
        except ValueError:
            return None
        return {
            "action": action, "pillar": pillar, "token_id": token_id,
            "tick_lower": tick_lower, "tick_upper": tick_upper,
        }
    # reject
    return {
        "action": action, "pillar": pillar, "token_id": token_id,
        "tick_lower": None, "tick_upper": None,
    }
