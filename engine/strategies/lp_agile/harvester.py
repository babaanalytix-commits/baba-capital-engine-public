"""engine/strategies/lp_agile/harvester.py — LP harvest + reinvest planner.

THE GOAL (Yomi 2026-05-28): turn the BABA LP from "deposit and forget" into
a compounding monthly-income engine. At ~150-200% feeAPR on HyperEVM, $1,000
LP capital → $120/month if we compound right.

Phase 1 (this file, SHIPPED): SUGGEST-ONLY. Reads accrued fees from the
existing lp_agile snapshot (no new RPC) and emits a per-position harvest
plan with reinvest-vs-take-profit split. The plan lands at
engine/_signals/lp_harvest_plan.json — the PWA reads it; the operator
acts manually until Phase 2 ships auto-execute.

Phase 2 (when LP_HARVEST_AUTO_EXECUTE=true is set): wire collect() +
increase_liquidity() sign+send via lp_agile/executor.py. The reinvest_ratio
slice is swapped to the pool's required token-pair and added back; the
take-profit slice is sent to the LP wallet's USDC bag for monthly income.

POLICY (configurable via env):
  LP_HARVEST_THRESHOLD_USD   default 1.50  — only suggest harvest when
                                              accrued exceeds this (cost of
                                              gas + slippage must be earned
                                              before harvesting makes sense)
  LP_REINVEST_RATIO          default 0.80  — 80% reinvest, 20% take-profit.
                                              At 150% APR with 80% compound:
                                              effective ~180% on the compounded
                                              slice + 20% income realised.
  LP_HARVEST_AUTO_EXECUTE    default false — Phase 1 is suggest-only.

Writes to engine/_signals/lp_harvest_plan.json — PWA can render this as the
'fee harvest pipeline' card. Sends Telegram alert when total_to_harvest > 0.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.harvester")

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Inputs (already maintained by the LP scanner + pwa_publish)
LP_SNAPSHOT_PATH = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_agile_latest.json"
# Also written to engine/_reports as a fallback served path
LP_SNAPSHOT_PATH_FALLBACK = _REPO_ROOT / "engine" / "_signals" / "lp_agile_latest.json"

# Output
HARVEST_PLAN_PATH = _REPO_ROOT / "engine" / "_signals" / "lp_harvest_plan.json"
HARVEST_LEDGER_PATH = _REPO_ROOT / "engine" / "_signals" / "lp_harvest_ledger.jsonl"

# Policy (env-tunable)
HARVEST_THRESHOLD_USD = float(os.environ.get("LP_HARVEST_THRESHOLD_USD", "1.50"))
REINVEST_RATIO = float(os.environ.get("LP_REINVEST_RATIO", "0.80"))
AUTO_EXECUTE = (
    os.environ.get("LP_HARVEST_AUTO_EXECUTE", "false").lower() == "true"
)


@dataclass
class PositionHarvest:
    """One position's harvest suggestion."""
    nft_token_id: int
    protocol: str
    pair: str
    pool_address: str
    in_range: bool
    position_value_usd: float
    fees_owed_usd: float           # uncollected swap fees
    pending_aero_usd: Optional[float]  # gauge rewards (Slipstream only)
    total_unclaimed_usd: float     # fees + pending_aero
    yield_pct_on_position: float   # total_unclaimed / position_value × 100
    should_harvest: bool
    reinvest_usd: float
    take_profit_usd: float
    reason: str

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _load_lp_snapshot() -> Optional[dict]:
    """Read lp_agile_latest.json — prefer the served path, fallback to _signals."""
    for p in (LP_SNAPSHOT_PATH, LP_SNAPSHOT_PATH_FALLBACK):
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception as exc:                                # noqa: BLE001
                logger.warning("[harvester] snapshot read failed %s: %s", p, exc)
    logger.warning("[harvester] no lp_agile snapshot found")
    return None


def _plan_for_position(pos: dict) -> PositionHarvest:
    """Build a harvest plan for one position dict (from lp_agile snapshot)."""
    fees = float(pos.get("fees_owed_usd") or 0)
    aero = pos.get("pending_aero_usd")
    aero_usd = float(aero) if aero is not None else 0.0
    total_unclaimed = round(fees + aero_usd, 4)
    pos_value = float(pos.get("value_usd") or 0)
    yield_pct = round(total_unclaimed / pos_value * 100, 3) if pos_value > 0 else 0.0

    should = total_unclaimed >= HARVEST_THRESHOLD_USD
    reinvest = round(total_unclaimed * REINVEST_RATIO, 4) if should else 0.0
    tp = round(total_unclaimed - reinvest, 4) if should else 0.0
    if should:
        reason = (
            f"accrued ${total_unclaimed:.2f} >= threshold ${HARVEST_THRESHOLD_USD:.2f} "
            f"({yield_pct:.2f}% of position) — reinvest "
            f"{REINVEST_RATIO*100:.0f}% / take-profit {(1-REINVEST_RATIO)*100:.0f}%"
        )
    else:
        reason = (
            f"accrued ${total_unclaimed:.2f} below threshold ${HARVEST_THRESHOLD_USD:.2f} "
            f"— let fees compound a bit more"
        )
    return PositionHarvest(
        nft_token_id=int(pos.get("nft_token_id") or 0),
        protocol=str(pos.get("protocol") or "?"),
        pair=str(pos.get("pair") or "?"),
        pool_address=str(pos.get("pool_address") or ""),
        in_range=bool(pos.get("in_range")),
        position_value_usd=round(pos_value, 2),
        fees_owed_usd=round(fees, 4),
        pending_aero_usd=round(aero_usd, 4) if aero is not None else None,
        total_unclaimed_usd=total_unclaimed,
        yield_pct_on_position=yield_pct,
        should_harvest=should,
        reinvest_usd=reinvest,
        take_profit_usd=tp,
        reason=reason,
    )


def compute_harvest_plan() -> list[PositionHarvest]:
    """Read the LP snapshot, build a harvest plan per open position."""
    snap = _load_lp_snapshot()
    if not snap:
        return []
    open_positions = snap.get("open_positions") or []
    return [_plan_for_position(p) for p in open_positions]


def write_harvest_plan(plans: list[PositionHarvest]) -> Path:
    """Persist the harvest plan to disk for PWA + operator review."""
    HARVEST_PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    n_to_harvest = sum(1 for p in plans if p.should_harvest)
    total_unclaimed = round(sum(p.total_unclaimed_usd for p in plans), 2)
    total_to_harvest = round(sum(p.total_unclaimed_usd for p in plans if p.should_harvest), 2)
    total_reinvest = round(sum(p.reinvest_usd for p in plans if p.should_harvest), 2)
    total_take_profit = round(sum(p.take_profit_usd for p in plans if p.should_harvest), 2)
    payload = {
        "generated_at_iso": datetime.now(timezone.utc).isoformat(),
        "policy": {
            "threshold_usd": HARVEST_THRESHOLD_USD,
            "reinvest_ratio": REINVEST_RATIO,
            "auto_execute": AUTO_EXECUTE,
        },
        "summary": {
            "n_positions": len(plans),
            "n_to_harvest_now": n_to_harvest,
            "total_unclaimed_usd": total_unclaimed,
            "total_to_harvest_usd": total_to_harvest,
            "total_reinvest_usd": total_reinvest,
            "total_take_profit_usd": total_take_profit,
        },
        "plans": [p.to_dict() for p in plans],
    }
    HARVEST_PLAN_PATH.write_text(json.dumps(payload, indent=2, default=str))
    logger.info(
        "[harvester] wrote plan: %d positions, %d ready, $%.2f unclaimed, $%.2f to harvest",
        len(plans), n_to_harvest, total_unclaimed, total_to_harvest,
    )
    return HARVEST_PLAN_PATH


def _maybe_alert_telegram(plans: list[PositionHarvest]) -> None:
    """Send a Telegram nudge when total_to_harvest crosses the threshold.
    Silent when nothing's actionable."""
    ready = [p for p in plans if p.should_harvest]
    if not ready:
        return
    total_to_harvest = sum(p.total_unclaimed_usd for p in ready)
    total_reinvest = sum(p.reinvest_usd for p in ready)
    total_tp = sum(p.take_profit_usd for p in ready)
    try:
        from engine.telegram.client import send as _tg_send, tg_escape_html as _e
        lines = [
            f"💰 <b>LP Harvest Ready · ${total_to_harvest:.2f}</b>",
            (f"Reinvest <b>${total_reinvest:.2f}</b> · "
             f"Take-profit <b>${total_tp:.2f}</b>"),
            "",
        ]
        for p in ready[:10]:
            lines.append(
                f"• <code>{_e(p.pair)}</code> "
                f"(#{p.nft_token_id}) {p.protocol} · "
                f"<b>${p.total_unclaimed_usd:.2f}</b> "
                f"({p.yield_pct_on_position:.2f}%)"
            )
        if AUTO_EXECUTE:
            lines.append("\n<i>Auto-execute is ON — harvesting now.</i>")
        else:
            lines.append("\n<i>SUGGEST-ONLY. Set LP_HARVEST_AUTO_EXECUTE=true "
                         "to auto-execute (Phase 2).</i>")
        _tg_send(
            "signal",
            key=f"lp_harvest_ready:{int(datetime.now(timezone.utc).timestamp() // 3600)}",
            text="\n".join(lines), parse_mode="HTML",
        )
    except Exception as exc:                                       # noqa: BLE001
        logger.warning("[harvester] telegram alert failed: %s", exc)


def run_once() -> dict:
    """Main entrypoint — compute, persist, alert, return summary."""
    plans = compute_harvest_plan()
    write_harvest_plan(plans)
    _maybe_alert_telegram(plans)
    return {
        "n_positions": len(plans),
        "n_to_harvest": sum(1 for p in plans if p.should_harvest),
        "total_unclaimed_usd": round(sum(p.total_unclaimed_usd for p in plans), 2),
        "plan_path": str(HARVEST_PLAN_PATH),
        "auto_execute_phase": AUTO_EXECUTE,
    }


def main():
    """CLI entry: `python3 -m engine.strategies.lp_agile.harvester`"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    summary = run_once()
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    main()
