#!/usr/bin/env python3
"""engine/allocator/position_ev.py — read-only per-position EV-per-$/day ranker.

THE WHY (memory: feedback_capital_allocator_priority + feedback_liquidation_creates_naked_leg):
  Phase 0 (this module): SEE which open positions are returning the least per
    dollar of margin per day. When a venue tips into URGENT margin (>75%), this
    is the answer to "close which one first to free room without bleeding alpha".
  Phase 1: surfaced inside margin_tier_alerter URGENT/CRITICAL Telegram.
  Phase 2 (future, gated): auto-close worst position on CRITICAL once we've
    watched the ranker call shots correctly for a week.

WHAT IT READS:
  - engine/_registries/{md,carry,oracle}.db — open positions + strategy_id
  - ops/treasury/snapshot_latest.json — live unrealised PnL per position
  - per_strategy_stats(days=30) — strategy expectancy / sharpe for tie-breaking

WHAT IT COMPUTES (simple, conservative):
  realised_ev_per_dollar_per_day =
    unrealised_pnl_usd / size_usd / max(days_held, 0.5)

  We use a 12h floor on days_held because per-hour PnL of a 10-min-old position
  is meaningless noise. Lower scores = WORSE position = better close candidate.

  When days_held < 12h AND we have strategy_id history, we additionally pull
  the strategy's 30-day expectancy as a forward-EV proxy.

WHAT IT DOES NOT DO (Phase 0 deliberate scope):
  - Does NOT close positions. Read-only advisory.
  - Does NOT respect delta-neutral pairing (engine CARRY pillar is empty right
    now; if/when it has hedged pairs, NEVER suggest closing one leg without
    its hedge — memory feedback_all_execution_delta_neutral).
  - Does NOT model funding income (engine MD positions are directional, not
    hedged carries — funding is captured by the carry pillar separately).

CLI:
    PYTHONPATH=. python3 -m engine.allocator.position_ev rank
    PYTHONPATH=. python3 -m engine.allocator.position_ev rank --venue grvt
    PYTHONPATH=. python3 -m engine.allocator.position_ev worst --venue grvt
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.allocator.position_ev")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REGISTRIES_DIR = _REPO_ROOT / "engine" / "_registries"
_SNAPSHOT_PATH = _REPO_ROOT / "ops" / "treasury" / "snapshot_latest.json"

# Pillars we look at. Carry is hedged-pair territory; once it has pairs we'll
# need to extend the ranker to score pair-level EV (delta-neutral never breaks
# one leg). Until then it's safe to include — list_open() returns [].
_PILLARS = ("md", "carry", "oracle")

# Floor on days_held in the EV-per-day denominator. Avoids per-second noise
# on freshly-opened positions getting them flagged as catastrophic losers.
_MIN_DAYS_HELD_FLOOR = 0.5  # 12h


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_decimal(s, default: Decimal = Decimal("0")) -> Decimal:
    if s is None or s == "":
        return default
    try:
        return Decimal(str(s))
    except (InvalidOperation, ValueError):
        return default


def _load_open_positions_from_db(pillar: str) -> list[dict]:
    """Return list of open position dicts from one pillar's registry."""
    db_path = _REGISTRIES_DIR / f"{pillar}.db"
    if not db_path.exists():
        return []
    out: list[dict] = []
    try:
        con = sqlite3.connect(str(db_path), timeout=10)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, pillar, venue, asset, side, size_usd, entry_price, "
            "       opened_at_iso, strategy_id "
            "FROM positions WHERE status = 'open' "
            "ORDER BY opened_at_iso DESC"
        ).fetchall()
        con.close()
        for r in rows:
            out.append({
                "pillar": pillar,
                "id": int(r["id"]),
                "venue": r["venue"],
                "asset": r["asset"],
                "side": r["side"],
                "size_usd": _safe_decimal(r["size_usd"]),
                "entry_price": _safe_decimal(r["entry_price"]),
                "opened_at_iso": r["opened_at_iso"],
                "strategy_id": r["strategy_id"] or "manual",
            })
    except Exception as exc:
        logger.warning(f"[position_ev] read {pillar} failed: {exc}")
    return out


def _load_snapshot_positions() -> dict[tuple, dict]:
    """Index treasury snapshot positions by (venue, asset, side) for fast joins.

    Snapshot position keys: symbol, side, size_usd, entry_price, mark_price,
    unrealised_pnl_usd. We rely on (venue, asset, side) being a unique enough
    key — there's at most one open per (venue,asset,side) in normal operation.
    """
    if not _SNAPSHOT_PATH.exists():
        return {}
    try:
        snap = json.loads(_SNAPSHOT_PATH.read_text())
    except Exception as exc:
        logger.warning(f"[position_ev] snapshot read failed: {exc}")
        return {}
    out: dict[tuple, dict] = {}
    for v in snap.get("venues") or []:
        venue = v.get("venue", "")
        for p in v.get("positions") or []:
            sym = (p.get("symbol") or "").upper()
            # Strip common perp suffixes — LONGEST FIRST so e.g. ZRO-USD-PERP
            # doesn't get trimmed to ZRO-USD by hitting -PERP first.
            for suf in ("-USD-PERP", "_USDT_PERP", "_USD_PERP", "-USD", "-PERP"):
                if sym.endswith(suf):
                    sym = sym[: -len(suf)]
                    break
            side = (p.get("side") or "").lower()
            out[(venue, sym, side)] = {
                "size_usd": _safe_decimal(p.get("size_usd")),
                "entry_price": _safe_decimal(p.get("entry_price")),
                "mark_price": _safe_decimal(p.get("mark_price")),
                "unrealised_pnl_usd": _safe_decimal(p.get("unrealised_pnl_usd")),
            }
    return out


def _load_strategy_stats() -> dict[str, dict]:
    """Pull 30-day per-strategy stats from MD registry (the only one currently
    has stats). Returns {strategy_id: {expectancy_usd, sharpe_proxy, trades}}."""
    out: dict[str, dict] = {}
    try:
        # Import lazily — engine import side-effects can be heavy
        sys.path.insert(0, str(_REPO_ROOT))
        from engine.pillars.md.registry import md_registry  # type: ignore
        stats = md_registry().per_strategy_stats(days=30)
        for s in stats:
            out[s["strategy_id"]] = {
                "expectancy_usd": float(s.get("expectancy_usd") or 0),
                "sharpe_proxy": float(s.get("sharpe_proxy") or 0),
                "trades": int(s.get("trades") or 0),
                "win_rate": float(s.get("win_rate") or 0),
                "graduation_status": s.get("graduation_status", "needs_more_data"),
            }
    except Exception as exc:
        logger.warning(f"[position_ev] strategy stats unavailable: {exc}")
    return out


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

@dataclass
class PositionScore:
    pillar: str
    id: int
    venue: str
    asset: str
    side: str
    size_usd: Decimal
    days_held: float
    unrealised_pnl_usd: Decimal
    realised_ev_per_dollar_per_day: float
    annualised_realised_apr_pct: float
    strategy_id: str
    strategy_expectancy_usd: Optional[float]
    strategy_sharpe: Optional[float]
    strategy_graduation_status: Optional[str]
    score: float  # final score; LOWER = WORSE = better close candidate
    notes: list[str]

    def to_dict(self) -> dict:
        return {
            "pillar": self.pillar,
            "id": self.id,
            "venue": self.venue,
            "asset": self.asset,
            "side": self.side,
            "size_usd": float(self.size_usd),
            "days_held": round(self.days_held, 3),
            "unrealised_pnl_usd": float(self.unrealised_pnl_usd),
            "realised_ev_per_dollar_per_day": self.realised_ev_per_dollar_per_day,
            "annualised_realised_apr_pct": self.annualised_realised_apr_pct,
            "strategy_id": self.strategy_id,
            "strategy_expectancy_usd": self.strategy_expectancy_usd,
            "strategy_sharpe": self.strategy_sharpe,
            "strategy_graduation_status": self.strategy_graduation_status,
            "score": self.score,
            "notes": self.notes,
        }


def _score_position(
    p: dict,
    snap_by_key: dict[tuple, dict],
    strat_stats: dict[str, dict],
    now_utc: datetime,
) -> PositionScore:
    """Combine registry row + snapshot mark + strategy stats into a score."""
    notes: list[str] = []
    venue = p["venue"]
    asset = p["asset"].upper()
    side = (p["side"] or "").lower()
    size_usd = p["size_usd"] or Decimal("0")

    # Days held
    try:
        opened = datetime.fromisoformat(p["opened_at_iso"])
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        days_held = max((now_utc - opened).total_seconds() / 86400.0, 0.0)
    except Exception:
        days_held = 0.0
        notes.append("could not parse opened_at_iso")

    # Snapshot match for unrealised PnL
    snap = snap_by_key.get((venue, asset, side))
    if snap is None:
        # Try matching just by asset+side without venue — sometimes Polymarket
        # positions aren't in the perp treasury snapshot
        unrealised = Decimal("0")
        if venue == "polymarket":
            notes.append("polymarket position — not tracked in treasury snapshot (perp-only)")
        else:
            notes.append(f"no live snapshot match for ({venue}, {asset}, {side}) — uPnL set to 0")
    else:
        unrealised = snap["unrealised_pnl_usd"]

    # Realised EV-per-$/day (denominator floor avoids 5-minute-old noise)
    days_for_denom = max(days_held, _MIN_DAYS_HELD_FLOOR)
    if size_usd > 0:
        realised_ev = float(unrealised) / float(size_usd) / days_for_denom
    else:
        realised_ev = 0.0
        notes.append("size_usd is zero — cannot score")

    annualised_apr = realised_ev * 365 * 100  # %

    # Strategy stats lookup — confluence strategies have comma-joined IDs;
    # take the first as the primary contributor for stats lookup
    strat_key = p["strategy_id"]
    if strat_key.startswith("confluence:"):
        strat_key = strat_key.split(":", 1)[1].split(",")[0]
    stats = strat_stats.get(strat_key)
    strat_exp = stats["expectancy_usd"] if stats else None
    strat_sharpe = stats["sharpe_proxy"] if stats else None
    strat_grad = stats["graduation_status"] if stats else None

    # Composite score:
    #   - Primary signal is realised_ev (LOWER = worse).
    #   - When days_held < floor, we lean MORE on strategy expectancy (a stale
    #     position with bad strategy history is a worse hold than a fresh
    #     position from a probation strategy).
    #   - We do NOT use sharpe_proxy in the score (too noisy at small samples)
    #     but expose it for the operator.
    if days_held < _MIN_DAYS_HELD_FLOOR and strat_exp is not None:
        # Score is half realised (per-dollar-per-day), half strategy expectancy
        # (per-trade, but in $). Normalise expectancy to per-dollar-per-day
        # using size_usd as the denominator + an assumed 1-day trade horizon.
        if size_usd > 0:
            normalised_strat = strat_exp / float(size_usd)
        else:
            normalised_strat = 0.0
        score = 0.5 * realised_ev + 0.5 * normalised_strat
        notes.append(f"freshly-opened (<{_MIN_DAYS_HELD_FLOOR}d); strategy_id "
                     f"expectancy ${strat_exp:.3f}/trade blended in")
    else:
        score = realised_ev

    # Annotate underperforming strategies
    if strat_grad == "underperforming":
        notes.append(f"⚠️ strategy {strat_key} flagged UNDERPERFORMING (≥30 trades, PF≤1.0 or expectancy≤0)")
    elif strat_grad == "needs_more_data":
        notes.append(f"strategy {strat_key} has <10 trades — insufficient sample")

    # Polymarket positions: rank them last for close-suggestion purposes when
    # the alert is venue-scoped — closing a Polymarket position frees zero
    # perp margin on HL/GRVT/Pacifica.
    if p["pillar"] == "oracle" and venue == "polymarket":
        notes.append("polymarket — closing does NOT free perp margin")

    return PositionScore(
        pillar=p["pillar"],
        id=p["id"],
        venue=venue,
        asset=asset,
        side=side,
        size_usd=size_usd,
        days_held=days_held,
        unrealised_pnl_usd=unrealised,
        realised_ev_per_dollar_per_day=realised_ev,
        annualised_realised_apr_pct=annualised_apr,
        strategy_id=p["strategy_id"],
        strategy_expectancy_usd=strat_exp,
        strategy_sharpe=strat_sharpe,
        strategy_graduation_status=strat_grad,
        score=score,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rank_all_open(*, venue: Optional[str] = None) -> list[PositionScore]:
    """Return all open positions scored, sorted WORST-first (= best close candidate).

    If `venue` is provided, only positions on that venue are returned.
    Polymarket positions are always returned LAST in venue=None mode (they
    don't free perp margin), but when explicitly scoped to venue=polymarket
    they're ranked normally.
    """
    now = datetime.now(timezone.utc)
    snap = _load_snapshot_positions()
    strat_stats = _load_strategy_stats()
    all_positions: list[dict] = []
    for pillar in _PILLARS:
        all_positions.extend(_load_open_positions_from_db(pillar))

    if venue:
        # Venue-scoped: only include positions on that venue
        all_positions = [p for p in all_positions if p["venue"] == venue]

    scored = [_score_position(p, snap, strat_stats, now) for p in all_positions]

    if not venue:
        # Push polymarket to end (zero perp-margin relief)
        non_pm = [s for s in scored if s.venue != "polymarket"]
        pm = [s for s in scored if s.venue == "polymarket"]
        non_pm.sort(key=lambda s: s.score)
        pm.sort(key=lambda s: s.score)
        scored = non_pm + pm
    else:
        scored.sort(key=lambda s: s.score)

    return scored


def worst_for_venue(venue: str) -> Optional[PositionScore]:
    """Return the single worst-EV position on a venue, or None if no positions."""
    ranked = rank_all_open(venue=venue)
    return ranked[0] if ranked else None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_rank(ranked: list[PositionScore], limit: int = 30) -> None:
    if not ranked:
        print("No open positions to rank.")
        return
    print(f"\n{'='*108}")
    print(f"  POSITIONS RANKED BY EV-PER-$/DAY (worst-EV first = best close candidate)")
    print(f"{'='*108}")
    print(f"{'#':<3} {'PILLAR':<7} {'ID':<5} {'VENUE':<11} {'ASSET':<10} {'SIDE':<5} "
          f"{'SIZE':>7} {'DAYS':>5} {'uPnL':>9} {'EV/$/D':>11} {'APR%':>9} {'STRATEGY':<30}")
    print("-" * 108)
    for i, s in enumerate(ranked[:limit], 1):
        strat_short = s.strategy_id[:28] + "..." if len(s.strategy_id) > 30 else s.strategy_id
        print(f"{i:<3} {s.pillar:<7} {s.id:<5} {s.venue:<11} {s.asset:<10} {s.side:<5} "
              f"${float(s.size_usd):>5.0f} {s.days_held:>5.2f} "
              f"${float(s.unrealised_pnl_usd):>+8.3f} "
              f"{s.realised_ev_per_dollar_per_day:>+11.6f} "
              f"{s.annualised_realised_apr_pct:>+8.1f}% "
              f"{strat_short}")
        for note in s.notes:
            print(f"      └ {note}")


def cli_rank(args):
    ranked = rank_all_open(venue=args.venue)
    if args.json:
        print(json.dumps([s.to_dict() for s in ranked], indent=2, default=str))
    else:
        _print_rank(ranked, limit=args.limit)


def cli_worst(args):
    if not args.venue:
        print("error: --venue is required for `worst`", file=sys.stderr)
        sys.exit(2)
    s = worst_for_venue(args.venue)
    if s is None:
        print(f"No open positions on {args.venue}")
        sys.exit(1)
    if args.json:
        print(json.dumps(s.to_dict(), indent=2, default=str))
    else:
        print(f"\nWorst-EV position on {args.venue}:")
        print(f"  {s.pillar}#{s.id}  {s.asset} {s.side}  ${float(s.size_usd):.0f}  "
              f"held {s.days_held:.2f}d")
        print(f"  uPnL ${float(s.unrealised_pnl_usd):+.3f}  "
              f"EV/$/d {s.realised_ev_per_dollar_per_day:+.6f}  "
              f"realised APR {s.annualised_realised_apr_pct:+.1f}%")
        print(f"  strategy: {s.strategy_id}")
        for note in s.notes:
            print(f"    • {note}")


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description="Open-position EV ranker (read-only)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_rank = sub.add_parser("rank", help="rank all open positions by EV/$/day (worst-first)")
    p_rank.add_argument("--venue", help="filter to one venue (hyperliquid|grvt|pacifica|polymarket)")
    p_rank.add_argument("--limit", type=int, default=30)
    p_rank.add_argument("--json", action="store_true")
    p_rank.set_defaults(func=cli_rank)

    p_worst = sub.add_parser("worst", help="show only the single worst-EV position on a venue")
    p_worst.add_argument("--venue", required=True,
                         help="venue to scope (hyperliquid|grvt|pacifica|polymarket)")
    p_worst.add_argument("--json", action="store_true")
    p_worst.set_defaults(func=cli_worst)

    args = ap.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
