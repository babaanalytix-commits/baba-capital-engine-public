"""engine/strategies/lp_agile/lp_rebalance_alerter.py — capital movement
recommendations via Telegram (alert-only).

Yomi 2026-06-04 #420: "There will be no cross-chain automated rebalancing
for now. Just to reduce risk, but we need to be able to have an alert
recommending how much to move from where to where."

This wraps the existing capital_optimizer.propose() (which already emits
"add liquidity to X by $Y / remove $Y from Z" proposals) and fires them
to Telegram on a daily cadence, with dedup so the same recommendation
doesn't spam.

Architecture:
  capital_optimizer.propose() →
    {proposals: [{action, position_id, amount_usd, reason, ...}, ...]}
  ↓
  Filter to actionable (amount_usd ≥ LP_OPT_MIN_GAP_USD, same chain)
  ↓
  Dedup against state file (proposal hash → last alerted ts)
  ↓
  Telegram digest (operator only) when ≥1 new actionable proposal

Daily cadence is enough — capital movements are not time-critical and we
don't want to spam during volatile APR readings.

Pairs naturally with lp_apr_scanner.py (which handles cross-chain
recommendations). Combined coverage:
  - Intra-chain: this module (uses capital_optimizer's APR-vs-APR scoring)
  - Cross-chain: lp_apr_scanner.py (uses defillama best-of-chain)

Shipped 2026-06-04 #420.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("engine.strategies.lp_agile.lp_rebalance_alerter")

_REPO = Path(__file__).resolve().parents[3]
DEDUP_PATH = _REPO / "engine" / "_state" / "lp_rebalance_alert_dedup.json"
LOG_PATH = _REPO / "engine" / "_signals" / "lp_rebalance_alerter_audit.jsonl"

# Cooldown per unique recommendation
DEDUP_COOLDOWN_HOURS = float(os.environ.get("LP_REBAL_ALERT_COOLDOWN_HOURS", "24"))
MIN_PROPOSAL_USD = float(os.environ.get("LP_OPT_MIN_GAP_USD", "10"))


def _log(event: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    event["ts_iso"] = datetime.now(timezone.utc).isoformat()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(event, default=str) + "\n")


def _load_dedup() -> dict:
    try:
        return json.loads(DEDUP_PATH.read_text())
    except Exception:
        return {}


def _save_dedup(d: dict) -> None:
    DEDUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEDUP_PATH.write_text(json.dumps(d, indent=2))


def _alert_telegram(text: str) -> None:
    try:
        from engine.telegram.client import send
        send("signal", key=f"lp_rebal_alert:{int(time.time())}",
             text=text, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Telegram alert failed: %s", exc)


def _proposal_hash(p: dict) -> str:
    """Fingerprint a proposal — same chain + position + bucket of amount → same hash."""
    # Bucket amount in $50 chunks so small fluctuations don't cause re-alerts
    bucket = int((p.get("amount_usd") or 0) // 50) * 50
    seed = (
        f"{p.get('chain', '')}|{p.get('action', '')}|"
        f"{p.get('position_id', '')}|{p.get('target_position_id', '')}|"
        f"{bucket}"
    )
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


def tick() -> dict:
    """One alerter cycle. Reads optimizer, sends Telegram digest if new."""
    started = time.time()
    out = {
        "ts_iso": datetime.now(timezone.utc).isoformat(),
        "proposals_total": 0,
        "proposals_actionable": 0,
        "proposals_new": 0,
        "alert_fired": False,
    }

    try:
        from engine.strategies.lp_agile.capital_optimizer import propose
        optimizer_out = propose(write_journal=False)
    except Exception as exc:
        out["error"] = f"optimizer failed: {exc}"
        _log({"action": "optimizer_failed", **out})
        return out

    proposals = optimizer_out.get("proposals") or []
    out["proposals_total"] = len(proposals)

    # Filter to actionable (≥ min size)
    actionable = [
        p for p in proposals
        if (p.get("amount_usd") or 0) >= MIN_PROPOSAL_USD
    ]
    out["proposals_actionable"] = len(actionable)

    if not actionable:
        _log({"action": "no_actionable", **out})
        return out

    # Dedup
    dedup = _load_dedup()
    now = time.time()
    new_proposals = []
    for p in actionable:
        h = _proposal_hash(p)
        last = dedup.get(h, 0)
        elapsed_h = (now - last) / 3600
        if elapsed_h >= DEDUP_COOLDOWN_HOURS:
            new_proposals.append(p)
            dedup[h] = now

    out["proposals_new"] = len(new_proposals)
    if not new_proposals:
        _log({"action": "all_dedup_silenced", **out})
        return out

    # Build Telegram digest
    by_chain = {}
    for p in new_proposals:
        chain = p.get("chain") or "unknown"
        by_chain.setdefault(chain, []).append(p)

    lines = ["💡 <b>LP capital rebalance recommendations</b>", ""]
    for chain, items in by_chain.items():
        lines.append(f"<b>{chain.upper()}</b> (intra-chain — same wallet):")
        for p in items[:5]:  # cap to 5 per chain
            action = p.get("action", "")
            amt = p.get("amount_usd", 0)
            pair = p.get("pair") or p.get("symbol") or "?"
            reason = p.get("reason") or ""
            lines.append(
                f"  • {action}: ${amt:.2f} {pair} "
                f"<i>({reason[:80]})</i>"
            )
        lines.append("")
    lines.append(
        "<i>Alert-only. Execute manually via prjx/Aerodrome UIs or hold for "
        "auto-rebalance once thresholds proven.</i>"
    )
    _alert_telegram("\n".join(lines))
    out["alert_fired"] = True
    _save_dedup(dedup)
    _log({"action": "alert_fired", **out})
    out["duration_s"] = round(time.time() - started, 2)
    return out


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    print(json.dumps(tick(), indent=2, default=str))
