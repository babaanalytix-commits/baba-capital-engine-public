"""engine/strategies/lp_agile/dynamic_range_dispatcher.py — orchestrates
proactive LP range optimization across all managed positions.

Per chain posture (Yomi 2026-06-04 #420):
  HyperEVM (prjx): AUTO-execute. Gas is $0.005 — wrong calls cost almost
    nothing. Faster cadence (10-30 min). Lower confidence threshold.
  Base (slipstream): ALERT-ONLY initially. Higher gas + IL math. Slower
    cadence (4h). Operator-approves via Telegram tap.

Flow:
  1. Read all open managed_positions per chain
  2. For each → fetch live price + position state from chain snapshot
  3. Build proposal via dynamic_range_proposer.propose_for_position
  4. If fire AND chain in AUTO set AND not in cooldown → execute
     (calls existing rebalance_executor.run_once on the position)
  5. If fire AND chain in ALERT set → send Telegram with the proposal
  6. Persist summary

Standing rule: this dispatcher SHADOWS the existing reactive trigger
(rebalance_trigger.py). They coexist — reactive fires on drift,
proactive fires on price-action edge. Same downstream executor.

Shipped 2026-06-04 #420 part 2 (dynamic range optimization layer).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.dynamic_range_dispatcher")

_REPO = Path(__file__).resolve().parents[3]
MANAGED_DB = _REPO / "engine" / "_registries" / "lp_managed_positions.db"
SNAPSHOT_PATH = _REPO / "ops" / "pwa" / "serve" / "lp_agile_latest.json"
COOLDOWN_PATH = _REPO / "engine" / "_state" / "lp_dynamic_range_cooldown.json"
LOG_PATH = _REPO / "engine" / "_signals" / "lp_dynamic_range_audit.jsonl"
SUMMARY_PATH = _REPO / "engine" / "_state" / "lp_dynamic_range_latest.json"

# Knobs
AUTO_EXECUTE_HYPEREVM = os.environ.get(
    "LP_DYN_AUTO_EXECUTE_HYPEREVM", "true"
).lower() == "true"
AUTO_EXECUTE_BASE = os.environ.get(
    "LP_DYN_AUTO_EXECUTE_BASE", "false"
).lower() == "true"
COOLDOWN_HOURS_HYPEREVM = float(
    os.environ.get("LP_DYN_COOLDOWN_HOURS_HYPEREVM", "3")
)
COOLDOWN_HOURS_BASE = float(
    os.environ.get("LP_DYN_COOLDOWN_HOURS_BASE", "8")
)


def _log(event: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    event["ts_iso"] = datetime.now(timezone.utc).isoformat()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(event, default=str) + "\n")


def _persist_summary(d: dict) -> None:
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_PATH.write_text(json.dumps(d, indent=2, default=str))


def _load_cooldown() -> dict:
    try:
        return json.loads(COOLDOWN_PATH.read_text())
    except Exception:
        return {}


def _save_cooldown(d: dict) -> None:
    COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOLDOWN_PATH.write_text(json.dumps(d, indent=2))


def _alert_telegram(text: str) -> None:
    try:
        from engine.telegram.client import send
        send("signal", key=f"lp_dyn_range:{int(time.time())}",
             text=text, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Telegram alert failed: %s", exc)


def _read_open_positions(chain: str) -> list[dict]:
    try:
        conn = sqlite3.connect(str(MANAGED_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM managed_positions
               WHERE chain=? AND status='open'
                 AND current_nft_token_id IS NOT NULL""",
            (chain,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("managed_positions read failed: %s", exc)
        return []


def _read_live_state(nft_token_id: int) -> dict:
    """Pull live position state from chain snapshot."""
    try:
        d = json.loads(SNAPSHOT_PATH.read_text())
        for p in d.get("open_positions") or []:
            if int(p.get("nft_token_id") or 0) == int(nft_token_id):
                return p
    except Exception:
        pass
    return {}


def _enqueue_for_executor(proposal: dict) -> bool:
    """Append a trigger to the existing rebalance_executor queue.

    Pipe into rebalance_executor's existing queue rather than calling the
    chain RPC directly — this gives us idempotency + the executor's existing
    guardrails.
    """
    try:
        queue_path = _REPO / "engine" / "_signals" / "lp_rebalance_triggers.jsonl"
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        trigger = {
            "source": "dynamic_range_dispatcher",
            "ts_iso": datetime.now(timezone.utc).isoformat(),
            "nft_token_id": proposal["nft_token_id"],
            "pair": proposal["pair"],
            "chain": proposal["chain"],
            "protocol": proposal["protocol"],
            "new_tick_lower": proposal["new_tick_lower"],
            "new_tick_upper": proposal["new_tick_upper"],
            "reason": "dynamic_range_edge",
            "estimated_uplift_pct": proposal.get("expected_uplift_pct"),
            "payback_days": proposal.get("payback_days"),
            "price_action": proposal.get("price_action"),
        }
        with queue_path.open("a") as f:
            f.write(json.dumps(trigger, default=str) + "\n")
        return True
    except Exception as exc:
        logger.error("enqueue failed: %s", exc)
        return False


def _process_chain(chain: str, *, auto_execute: bool, cooldown_h: float) -> dict:
    """Process all open positions on a chain. Returns summary."""
    from engine.strategies.lp_agile.dynamic_range_proposer import (
        propose_for_position,
    )

    out = {
        "chain": chain,
        "auto_execute": auto_execute,
        "positions_checked": 0,
        "proposals_fired": 0,
        "proposals_blocked": 0,
        "proposals_skipped_cooldown": 0,
        "details": [],
    }

    positions = _read_open_positions(chain)
    out["positions_checked"] = len(positions)
    cooldown = _load_cooldown()
    now = time.time()

    for p in positions:
        nft = int(p.get("current_nft_token_id") or 0)
        pair = f"{p.get('token0_symbol')}/{p.get('token1_symbol')}"
        live = _read_live_state(nft)
        if not live:
            out["details"].append({
                "nft_token_id": nft, "pair": pair,
                "skip": "no_live_state_in_snapshot",
            })
            continue

        # Cooldown
        last = cooldown.get(str(nft), 0)
        if now - last < cooldown_h * 3600:
            out["proposals_skipped_cooldown"] += 1
            out["details"].append({
                "nft_token_id": nft, "pair": pair,
                "skip": f"cooldown ({(cooldown_h - (now - last) / 3600):.1f}h remaining)",
            })
            continue

        # Build proposal
        try:
            current_price = float(live.get("pool_price_now") or 0)
            current_tick_lower = int(live.get("tick_lower") or p.get("current_tick_lower") or 0)
            current_tick_upper = int(live.get("tick_upper") or p.get("current_tick_upper") or 0)
            value_usd = float(live.get("value_usd") or p.get("lifetime_capital_in_usd") or 0)
            fee_tier_bps = float(p.get("fee_tier") or 0) / 100  # uint24 → bps
            realized_apr = float(
                live.get("projected_apr_pct")
                or live.get("apy_total_pct")
                or 0
            )
            # Snapshot APR often stale/0 — fall back to fresh DeFiLlama
            # leaderboard (same source the picker uses for highest-APR
            # decisions).
            if realized_apr == 0:
                try:
                    from engine.strategies.lp_agile.idle_deploy_picker import (
                        _read_leaderboard_apr, _lookup_apr_for_pair,
                    )
                    leaderboard = _read_leaderboard_apr()
                    leaderboard_apr = _lookup_apr_for_pair(
                        chain, pair, leaderboard,
                    )
                    if leaderboard_apr > 0:
                        realized_apr = leaderboard_apr
                except Exception:
                    pass
            tick_spacing = abs(current_tick_upper - current_tick_lower) // 10 or 1

            # Snapshot has range_low/range_high in the same unit as
            # pool_price_now — pass them in so the proposer doesn't have
            # to derive prices from ticks (correct for mixed-decimal pairs).
            range_low = live.get("range_low")
            range_high = live.get("range_high")
            current_lower_override = (
                float(range_low) if range_low is not None else None
            )
            current_upper_override = (
                float(range_high) if range_high is not None else None
            )
            proposal = propose_for_position(
                nft_token_id=nft,
                pair=pair,
                chain=chain,
                protocol=p.get("protocol") or "",
                current_price=current_price,
                current_tick_lower=current_tick_lower,
                current_tick_upper=current_tick_upper,
                position_value_usd=value_usd,
                fee_tier_bps=fee_tier_bps,
                realized_apr_pct=realized_apr,
                tick_spacing=tick_spacing,
                current_lower_price_override=current_lower_override,
                current_upper_price_override=current_upper_override,
            )
            proposal_dict = {
                k: getattr(proposal, k)
                for k in proposal.__dataclass_fields__.keys()
            }
            row = {
                "nft_token_id": nft, "pair": pair,
                "fire": proposal.fire,
                "payback_days": proposal.payback_days,
                "threshold_days": proposal.threshold_days,
                "expected_uplift_pct": proposal.expected_uplift_pct,
                "block_reason": proposal.block_reason,
                "price_action_direction": (
                    proposal.price_action.get("trend_direction")
                    if proposal.price_action else None
                ),
                "price_action_confidence": (
                    proposal.price_action.get("confidence")
                    if proposal.price_action else None
                ),
            }

            if proposal.fire and auto_execute:
                ok = _enqueue_for_executor(proposal_dict)
                if ok:
                    cooldown[str(nft)] = now
                    out["proposals_fired"] += 1
                    row["action"] = "enqueued_for_executor"
                    _alert_telegram(
                        f"⚙️ <b>LP dynamic range adjust ({chain})</b>\n"
                        f"NFT: {nft} ({pair})\n"
                        f"Trend: {row['price_action_direction']} "
                        f"(conf {row['price_action_confidence']:.2f})\n"
                        f"Payback: {proposal.payback_days:.2f}d "
                        f"(threshold {proposal.threshold_days:.2f}d)\n"
                        f"Uplift: +{proposal.expected_uplift_pct:.1f}% APR\n"
                        f"<i>Queued for executor</i>"
                    )
                else:
                    row["action"] = "enqueue_failed"
            elif proposal.fire and not auto_execute:
                # Alert-only mode (Base)
                _alert_telegram(
                    f"📊 <b>LP dynamic range opportunity ({chain})</b>\n"
                    f"NFT: {nft} ({pair})\n"
                    f"Trend: {row['price_action_direction']} "
                    f"(conf {row['price_action_confidence']:.2f})\n"
                    f"Proposed range: ${proposal.new_lower_price:.4f} – "
                    f"${proposal.new_upper_price:.4f}\n"
                    f"Current: ${proposal.current_lower_price:.4f} – "
                    f"${proposal.current_upper_price:.4f}\n"
                    f"Payback: {proposal.payback_days:.2f}d\n"
                    f"Uplift: +{proposal.expected_uplift_pct:.1f}% APR\n"
                    f"Cost: ${proposal.total_cost_usd:.3f}\n"
                    f"<i>Alert-only on {chain}. Reply or PWA to approve.</i>"
                )
                cooldown[str(nft)] = now
                out["proposals_fired"] += 1
                row["action"] = "alert_sent"
            else:
                out["proposals_blocked"] += 1
                row["action"] = "blocked"

            out["details"].append(row)
        except Exception as exc:
            logger.exception("proposal failed for %s: %s", nft, exc)
            out["details"].append({
                "nft_token_id": nft, "pair": pair,
                "error": str(exc),
            })

    _save_cooldown(cooldown)
    return out


def tick() -> dict:
    """Run one cycle across both chains."""
    started = time.time()
    summary = {
        "ts_iso": datetime.now(timezone.utc).isoformat(),
        "ts": started,
    }
    summary["hyperevm"] = _process_chain(
        "hyperevm",
        auto_execute=AUTO_EXECUTE_HYPEREVM,
        cooldown_h=COOLDOWN_HOURS_HYPEREVM,
    )
    summary["base"] = _process_chain(
        "base",
        auto_execute=AUTO_EXECUTE_BASE,
        cooldown_h=COOLDOWN_HOURS_BASE,
    )
    summary["duration_s"] = round(time.time() - started, 2)
    _log({"action": "tick", **summary})
    _persist_summary(summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    out = tick()
    print(json.dumps(out, indent=2, default=str))
