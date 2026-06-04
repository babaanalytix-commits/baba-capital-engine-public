"""engine/strategies/lp_agile/daily_digest.py — daily LP Telegram digest (#122).

Single morning summary covering:
  - Open LP positions (pair, protocol·chain, value, in-range, current APR)
  - Accrued fees / pending rewards (AERO, HYPE, …)
  - Suggested harvest+reinvest plans (from harvester.py)
  - Trigger-engine decisions in the last 24h (from rebalance_trigger journal)
  - Capital-pool NAV snapshot (if pool ledger has contributors)

Runs once a day via launchd (06:30 Prague). Silent when there's nothing
actionable (≥1 position out-of-range OR ≥1 harvest plan above threshold OR
≥1 trigger from the trigger engine).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.daily_digest")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LP_LATEST = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_agile_latest.json"
_TRIGGER_JOURNAL = _REPO_ROOT / "engine" / "_signals" / "lp_rebalance_decisions.jsonl"
_HARVEST_PLAN = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_harvest_plans.json"


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _read_positions() -> list[dict]:
    if not _LP_LATEST.exists():
        return []
    try:
        d = json.loads(_LP_LATEST.read_text())
        return d.get("open_positions") or []
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("[lp_digest] failed lp_agile_latest read: %s", exc)
        return []


def _read_recent_trigger_decisions(hours: int = 24) -> list[dict]:
    if not _TRIGGER_JOURNAL.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out: list[dict] = []
    try:
        for ln in _TRIGGER_JOURNAL.read_text().splitlines()[-500:]:
            try:
                r = json.loads(ln)
                ts_iso = r.get("ts_iso")
                if not ts_iso:
                    continue
                ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
                if ts >= cutoff:
                    out.append(r)
            except Exception:
                continue
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("[lp_digest] trigger journal read failed: %s", exc)
    return out


def _read_harvest_plan() -> Optional[dict]:
    if not _HARVEST_PLAN.exists():
        return None
    try:
        return json.loads(_HARVEST_PLAN.read_text())
    except Exception:                                           # noqa: BLE001
        return None


def _read_capital_pool_state() -> Optional[dict]:
    """Compute current pool shares if any contributors registered."""
    try:
        from engine.strategies.lp_agile import capital_pool as cp
        contribs = cp.all_contributors()
        if not contribs:
            return None
        positions = _read_positions()
        nav = sum(float(p.get("value_usd") or 0) for p in positions)
        return cp.compute_shares(nav)
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("[lp_digest] capital pool read failed: %s", exc)
        return None


def build_digest_text() -> tuple[str, bool]:
    """Build the digest. Returns (text, has_actionable_content).

    has_actionable_content drives the silent-when-healthy mandate — when
    False, caller can skip Telegram send.
    """
    positions = _read_positions()
    triggers = _read_recent_trigger_decisions(24)
    harvest = _read_harvest_plan() or {}
    pool = _read_capital_pool_state()

    lines: list[str] = []
    lines.append("💧 <b>BABA LP — daily digest</b>")
    lines.append(
        f"<i>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC</i>"
    )
    lines.append("")

    # Positions block
    if not positions:
        lines.append("<b>Positions</b>")
        lines.append("  No open LP positions.")
    else:
        total_value = sum(float(p.get("value_usd") or 0) for p in positions)
        n_in_range = sum(1 for p in positions if p.get("in_range"))
        n_out = len(positions) - n_in_range
        lines.append(
            f"<b>Positions</b> ({len(positions)} · "
            f"<b>${total_value:.2f}</b> NAV · {n_in_range} in-range"
            + (f" · ⚠ {n_out} OUT" if n_out else "")
            + ")"
        )
        for p in positions[:8]:
            pair = _esc(p.get("pair") or "?")
            proto = _esc((p.get("protocol") or "?").lower())
            chain = _esc((p.get("chain") or "").lower())
            badge = f"{proto}" + (f"·{chain}" if chain else "")
            value = float(p.get("value_usd") or 0)
            apr = p.get("current_apr_pct")
            apr_str = f"{apr:.0f}% APR" if apr is not None else "APR n/a"
            in_range_emoji = "🟢" if p.get("in_range") else "🔴"
            lines.append(
                f"  {in_range_emoji} <code>{pair}</code> "
                f"<i>{badge}</i> · ${value:.2f} · {apr_str}"
            )

    # Trigger block
    if triggers:
        triggered = [d for d in triggers if d.get("triggered")]
        lines.append("")
        lines.append(
            f"<b>Rebalance triggers — last 24h</b> "
            f"({len(triggered)} fired · {len(triggers)} evaluated)"
        )
        for d in triggered[:5]:
            lines.append(
                f"  ⚡ <code>{_esc(d.get('pair','?'))}</code> "
                f"({_esc(d.get('trigger','?'))}) — {_esc(d.get('reason','')[:90])}"
            )

    # Harvest plans
    harvest_plans = (harvest or {}).get("plans") or []
    ready = [p for p in harvest_plans if p.get("should_harvest")]
    if ready:
        lines.append("")
        total_ready = sum(float(p.get("total_unclaimed_usd") or 0) for p in ready)
        lines.append(
            f"<b>Harvest ready</b> ({len(ready)} positions · "
            f"${total_ready:.2f} accrued)"
        )
        for p in ready[:5]:
            pair = _esc(p.get("pair") or "?")
            tot = float(p.get("total_unclaimed_usd") or 0)
            reinv = float(p.get("reinvest_usd") or 0)
            tp = float(p.get("tp_usd") or 0)
            lines.append(
                f"  💰 <code>{pair}</code> ${tot:.2f} → "
                f"${reinv:.2f} reinvest · ${tp:.2f} TP"
            )

    # 2026-05-30: managed-position view (Phase 4 of LP pivot). One row per
    # logical position with lifetime metrics rolled up across NFT rotations.
    try:
        from engine.strategies.lp_agile import managed_position as MP
        from engine.strategies.lp_agile import nav as NAV
        # #171 — refresh realized_apr_pct on every managed position before
        # we render. Capital optimizer reads the same column; keeping the
        # recompute on the daily-digest path means proposals always see
        # fresh APR. Recompute is read-mostly + only writes one column.
        try:
            MP.recompute_all_realized_apr()
        except Exception as exc:                                   # noqa: BLE001
            logger.warning("[lp_digest] realized-APR recompute failed: %s", exc)
        managed = MP.list_managed_positions(status="open")
        if managed:
            nav_breakdown = NAV.compute_pool_nav(include_breakdown=True)
            nav_by_id = {p["managed_position_id"]: p
                         for p in (nav_breakdown.get("positions") or [])}
            lines.append("")
            lines.append("<b>Managed positions (lifetime)</b>")
            for mp in managed[:8]:
                pair = (f"{mp.get('token0_symbol') or '?'}/"
                        f"{mp.get('token1_symbol') or '?'}")
                nav_row = nav_by_id.get(mp["id"]) or {}
                nav_usd = nav_row.get("nav_usd", 0.0) or 0.0
                fees = float(mp.get("lifetime_fees_collected_usd") or 0)
                gas = float(mp.get("lifetime_gas_usd") or 0)
                net_fees = fees - gas
                mutations_n = 0
                try:
                    mutations_n = len(MP.list_mutations(
                        managed_position_id=mp["id"], limit=999
                    ))
                except Exception:
                    pass
                lines.append(
                    f"  ◇ <code>{_esc(pair)}</code> "
                    f"fee={mp.get('fee_tier')}  "
                    f"NAV ${nav_usd:.2f}  ·  "
                    f"fees ${fees:.2f}  ·  gas ${gas:.2f}  "
                    f"(net ${net_fees:+.2f})  ·  "
                    f"mutations: {mutations_n}"
                )
            if nav_breakdown.get("warnings"):
                lines.append(
                    f"  <i>⚠ {len(nav_breakdown['warnings'])} NAV gap(s) — "
                    f"some live state missing</i>"
                )
    except Exception as exc:                                       # noqa: BLE001
        logger.warning("[lp_digest] managed view failed: %s", exc)

    # Capital pool (if any contributors)
    if pool and (pool.get("n_contributors") or 0) > 0:
        lines.append("")
        lines.append(
            f"<b>Capital pool</b> ({pool['n_contributors']} contributor"
            f"{'s' if pool['n_contributors']>1 else ''} · "
            f"${pool['total_nav_usd']:.2f} NAV · "
            f"${pool['yomi_cut_total_usd']:.2f} operator-fee accrued)"
        )

    text = "\n".join(lines)
    has_actionable = bool(
        (triggers and any(d.get("triggered") for d in triggers)) or
        ready or
        any(not p.get("in_range") for p in positions)
    )
    return text, has_actionable


def send_digest(*, force: bool = False) -> dict:
    """Send the digest. When force=False and content is silent-when-healthy,
    skip send and return {sent: False}. Otherwise post via Telegram."""
    text, has_actionable = build_digest_text()
    if not has_actionable and not force:
        logger.info("[lp_digest] nothing actionable — silent")
        return {"sent": False, "reason": "silent_when_healthy", "text_len": len(text)}
    try:
        from engine.telegram.client import send as _send
        _send("lp-digest", key="lp_digest:" + datetime.now(timezone.utc).strftime("%Y-%m-%d"),
              text=text, parse_mode="HTML")
        return {"sent": True, "text_len": len(text)}
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("[lp_digest] telegram send failed: %s", exc)
        return {"sent": False, "error": str(exc)[:200], "text_len": len(text)}


if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="send even when silent-when-healthy")
    ap.add_argument("--dry-run", action="store_true",
                    help="build digest text + print, no Telegram send")
    args = ap.parse_args()
    if args.dry_run:
        t, a = build_digest_text()
        print(t)
        print()
        print(f"[has_actionable={a}, len={len(t)}]")
    else:
        r = send_digest(force=args.force)
        print(json.dumps(r, indent=2, default=str))
