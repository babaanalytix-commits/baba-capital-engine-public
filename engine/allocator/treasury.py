"""engine/allocator/treasury.py — read-only capital allocation digest.

V0.3.7 — Phase 1: READ-ONLY view of capital across HL / Pacifica / GRVT
perps + HLP vault. Computes per-venue equity, free margin, % of total
liquid, drift from target bucket. Emits Telegram digest + JSON snapshot.

WHY READ-ONLY FIRST:
  Auto-execution of HLP↔perps transfers is irreversible + financial.
  Per `feedback_structural_changes_options_first` + the long memory of
  allocator bugs (#336, #386), the right path is:
    1. Phase 1 (this module): SEE the drift, suggest the move
    2. Phase 2 (V0.3.8): one-tap Telegram approval to fire the transfer
    3. Phase 3: full auto when track record proves the math is right

TARGETS:
  Default bucket weights — env-overridable:
  - HL_PERPS:    40%
  - HLP_VAULT:   20%
  - PACIFICA:    20%
  - GRVT:        20%

  Drift band: ±5% triggers a "REBALANCE SUGGESTED" call-out.

USAGE:
  python3 -m engine.allocator.treasury --once
  python3 -m engine.allocator.treasury --once --no-telegram
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from engine.core.types import Venue

logger = logging.getLogger("engine.allocator.treasury")

_REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOC_DIR = _REPO_ROOT / "engine" / "_reports"
ALLOC_DIR.mkdir(parents=True, exist_ok=True)
LATEST_PATH = ALLOC_DIR / "treasury_latest.json"

# 2026-06-04 (#416): V2 schema — 3-pillar portfolio (LP/MD/ORACLE) as the
# operational targets, plus legacy buckets tracked at 0% target for
# wind-down visibility (Pacifica idle, GRVT cold reserve, HLP deprecated).
# Per Yomi's locked target: LP 60 / MD 25 / ORACLE 15. Drift band 10%.
DEFAULT_BUCKETS = {
    # 3 PILLARS — the operational portfolio
    "lp":                Decimal("60"),
    "md_ai":             Decimal("25"),   # = HL master + sub-accounts when Track B ships
    "oracle":            Decimal("15"),
    # LEGACY/WIND-DOWN — target 0%, surfaced for transparency
    "pacifica":          Decimal("0"),    # wind down (per 2026-06-04 plan)
    "grvt":              Decimal("0"),    # cold reserve (per 2026-06-04 plan)
    "hlp_vault":         Decimal("0"),    # deprecated
}
DRIFT_BAND_PCT = Decimal("10")  # ±10% before flagging (was 5% in v1)


@dataclass
class BucketState:
    name: str
    equity_usd: Decimal
    free_usd: Decimal
    target_pct: Decimal
    note: str = ""

    @property
    def current_pct(self) -> Decimal:
        return self.equity_usd  # filled after total computed

    def actual_pct(self, total: Decimal) -> Decimal:
        if total <= 0:
            return Decimal("0")
        return self.equity_usd / total * Decimal("100")


def _safe_margin_summary(venue: Venue) -> Optional[dict]:
    """Best-effort margin pull. Returns None on any failure."""
    try:
        if venue == Venue.HYPERLIQUID:
            from engine.venues.hyperliquid import HyperliquidClient
            client = HyperliquidClient(dry_run=False)
        elif venue == Venue.PACIFICA:
            from engine.venues.pacifica import PacificaClient
            client = PacificaClient(dry_run=False)
        elif venue == Venue.GRVT:
            from engine.venues.grvt import GRVTClient
            client = GRVTClient(dry_run=False)
        else:
            return None
        adapter = client._get_adapter()  # noqa: SLF001
        if not hasattr(adapter, "get_margin_summary"):
            return None
        return dict(adapter.get_margin_summary() or {})
    except Exception as exc:
        logger.warning(f"[treasury] {venue.value} margin summary failed: {exc}")
        return None


def _safe_hlp_balance() -> Optional[Decimal]:
    """Read HLP vault balance.

    V0.4.4 (2026-05-20): prefer the dedicated hlp_position_latest.json
    written every 5min by com.baba.hlp-balance worker. Falls back to
    live adapter query if the file is missing or unreadable.

    Why file-first: the hlp-balance worker already does the live query
    correctly + writes the result; treasury re-doing it on every snapshot
    duplicates work + masked an empty $0 bucket today (file fresh, but
    adapter method-probe returned None silently → UNREACHABLE note swallowed).
    """
    import json
    hlp_file = Path("/Users/yomioguntona/baba/wealth-ecosystem"
                    "/ops/opportunities/hlp_position_latest.json")
    if hlp_file.exists():
        try:
            data = json.loads(hlp_file.read_text())
            # Common keys we've seen across versions of the writer
            for k in ("equity_usd", "balance_usd", "value_usd", "equity",
                      "balance", "vault_equity_usd", "user_equity_usd"):
                if k in data:
                    return Decimal(str(data[k]))
            # Nested under "hlp" or "vault" sometimes
            for outer in ("hlp", "vault", "position"):
                if outer in data and isinstance(data[outer], dict):
                    for k in ("equity_usd", "balance_usd", "value_usd",
                              "equity", "balance"):
                        if k in data[outer]:
                            return Decimal(str(data[outer][k]))
            logger.warning(
                f"[treasury] hlp_position_latest.json present but no known "
                f"value key found. Keys: {list(data.keys())}"
            )
        except Exception as exc:
            logger.warning(f"[treasury] hlp file parse failed: {exc}")

    # Fallback: live adapter query (legacy behaviour)
    try:
        from engine.venues.hyperliquid import HyperliquidClient
        client = HyperliquidClient(dry_run=False)
        adapter = client._get_adapter()  # noqa: SLF001
        for method in ("get_hlp_balance", "get_hlp_equity",
                       "get_vault_equity", "userVaultEquities"):
            if hasattr(adapter, method):
                val = getattr(adapter, method)()
                if val is None:
                    continue
                if isinstance(val, dict):
                    for k in ("equity_usd", "balance_usd", "value_usd",
                              "equity", "balance"):
                        if k in val:
                            return Decimal(str(val[k]))
                return Decimal(str(val))
        return None
    except Exception as exc:
        logger.warning(f"[treasury] HLP balance read failed: {exc}")
        return None


# ───────────────────────────────────────────────────────────────────────
# 2026-06-04 (#416) — V2 pillar equity readers
# ───────────────────────────────────────────────────────────────────────

def _lp_equity_usd() -> tuple[Decimal, Decimal, str]:
    """LP pillar equity = sum(open managed_positions.lifetime_capital_in_usd)
    + idle wallet balance on Base (USDC + cbBTC + ETH if any).

    Returns (equity_usd, free_usd_idle, note). Free = idle wallet only (the
    deployed capital isn't "free" until withdrawn). The reconciler keeps
    managed_positions in sync with chain truth on a 15-min cadence (#171).
    """
    import sqlite3
    deployed = Decimal("0")
    idle = Decimal("0")
    notes: list[str] = []

    # 1. Deployed LP capital from managed_positions registry
    db = _REPO_ROOT / "engine" / "_registries" / "lp_managed_positions.db"
    if db.exists():
        try:
            conn = sqlite3.connect(str(db))
            row = conn.execute(
                "SELECT COALESCE(SUM(lifetime_capital_in_usd), 0) "
                "FROM managed_positions WHERE status='open'"
            ).fetchone()
            conn.close()
            deployed = Decimal(str(row[0] or 0))
        except Exception as exc:
            notes.append(f"managed_positions read failed: {exc}")
    else:
        notes.append("lp_managed_positions.db not found")

    # 2. Idle balance on Base — read from dispatcher's most recent run
    idle_latest = _REPO_ROOT / "engine" / "_signals" / "lp_idle_deploy_latest.json"
    if idle_latest.exists():
        try:
            d = json.loads(idle_latest.read_text())
            wb = d.get("wallet_balance") or {}
            idle = (
                Decimal(str(wb.get("usdc_usd") or 0))
                + Decimal(str(wb.get("cbbtc_usd") or 0))
                + Decimal(str(wb.get("eth_usd") or 0))
            )
        except Exception as exc:
            notes.append(f"idle balance read failed: {exc}")

    equity = deployed + idle
    note = "; ".join(notes) if notes else ""
    return equity, idle, note


def _oracle_equity_usd() -> tuple[Decimal, Decimal, str]:
    """ORACLE pillar equity = Polymarket cash balance + sum of open
    position mark values.

    Cash comes from the canonical PWA snapshot (ops/pwa/serve/pwa_snapshot.json
    polymarket.total_cash_usd). Open positions from oracle.db.
    """
    import sqlite3
    cash = Decimal("0")
    positions_value = Decimal("0")
    notes: list[str] = []

    # 1. Polymarket cash from canonical snapshot (already updated by reconciler)
    for snap_path in (
        _REPO_ROOT / "ops" / "pwa" / "serve" / "pwa_snapshot.json",
        _REPO_ROOT / "engine" / "_reports" / "baba_app_snapshot.json",
    ):
        if snap_path.exists():
            try:
                d = json.loads(snap_path.read_text())
                pm = d.get("polymarket") or {}
                # Try multiple field names; whichever exists
                for k in (
                    "total_cash_usd", "cash_usd", "cash",
                    "available_balance_usd",
                ):
                    v = pm.get(k)
                    if v is not None:
                        cash = Decimal(str(v))
                        break
                if cash > 0:
                    break
            except Exception:
                pass

    # 2. Open ORACLE positions from oracle.db
    db = _REPO_ROOT / "engine" / "_registries" / "oracle.db"
    if db.exists():
        try:
            conn = sqlite3.connect(str(db))
            row = conn.execute(
                """SELECT COALESCE(SUM(size_usd), 0)
                   FROM positions WHERE status='open'"""
            ).fetchone()
            conn.close()
            positions_value = Decimal(str(row[0] or 0))
        except Exception as exc:
            notes.append(f"oracle.db read failed: {exc}")

    equity = cash + positions_value
    note = "; ".join(notes) if notes else ""
    return equity, cash, note


def _md_ai_equity_usd() -> tuple[Decimal, Decimal, str]:
    """MD AI pillar equity = HL master account value (including sub-accounts
    when Track B desks come online). Reuses the legacy HL margin reader.

    Returns (equity_usd, free_usd, note).
    """
    summary = _safe_margin_summary(Venue.HYPERLIQUID)
    if summary is None:
        return Decimal("0"), Decimal("0"), "UNREACHABLE — HL margin failed"
    equity = Decimal(str(summary.get("account_value", "0") or 0))
    free = Decimal(str(summary.get("free_margin", "0") or 0))
    return equity, free, ""


def _bucket_target(key: str) -> Decimal:
    env_key = f"BABA_BUCKET_{key.upper()}_PCT"
    raw = os.environ.get(env_key)
    if raw is not None:
        try:
            return Decimal(raw)
        except Exception:
            pass
    return DEFAULT_BUCKETS.get(key, Decimal("0"))


def build_snapshot() -> dict:
    """V2 — 3-pillar (LP/MD/ORACLE) portfolio snapshot with legacy buckets
    tracked at 0% for wind-down transparency. See #416 for the schema rationale.
    """
    now = datetime.now(timezone.utc)
    buckets: list[BucketState] = []

    # ─── 3 OPERATIONAL PILLARS ────────────────────────────────────────
    lp_eq, lp_free, lp_note = _lp_equity_usd()
    buckets.append(BucketState(
        name="lp",
        equity_usd=lp_eq, free_usd=lp_free,
        target_pct=_bucket_target("lp"),
        note=lp_note,
    ))

    md_eq, md_free, md_note = _md_ai_equity_usd()
    buckets.append(BucketState(
        name="md_ai",
        equity_usd=md_eq, free_usd=md_free,
        target_pct=_bucket_target("md_ai"),
        note=md_note,
    ))

    oracle_eq, oracle_free, oracle_note = _oracle_equity_usd()
    buckets.append(BucketState(
        name="oracle",
        equity_usd=oracle_eq, free_usd=oracle_free,
        target_pct=_bucket_target("oracle"),
        note=oracle_note,
    ))

    # ─── LEGACY/WIND-DOWN — surfaced for transparency, target 0% ─────
    for venue, key in (
        (Venue.PACIFICA, "pacifica"),
        (Venue.GRVT, "grvt"),
    ):
        summary = _safe_margin_summary(venue)
        equity = Decimal("0")
        free = Decimal("0")
        note = ""
        if summary is None:
            note = "UNREACHABLE — margin summary failed"
        else:
            equity = Decimal(str(summary.get("account_value", "0") or 0))
            free = Decimal(str(summary.get("free_margin", "0") or 0))
            if equity > 0:
                note = "WIND-DOWN — withdraw to LP or HL"
        buckets.append(BucketState(
            name=key, equity_usd=equity, free_usd=free,
            target_pct=_bucket_target(key), note=note,
        ))

    # HLP vault (deprecated — kept for visibility)
    hlp = _safe_hlp_balance()
    buckets.append(BucketState(
        name="hlp_vault",
        equity_usd=hlp if hlp is not None else Decimal("0"),
        free_usd=hlp if hlp is not None else Decimal("0"),
        target_pct=_bucket_target("hlp_vault"),
        note=("DEPRECATED — withdraw if non-zero" if hlp and hlp > 0
              else ("UNREACHABLE" if hlp is None else "")),
    ))

    total_equity = sum((b.equity_usd for b in buckets), Decimal("0"))
    total_free = sum((b.free_usd for b in buckets), Decimal("0"))

    bucket_rows: list[dict] = []
    rebalance_suggestions: list[str] = []
    # Build a quick map for under/over computation per pillar
    by_name = {b.name: b for b in buckets}
    for b in buckets:
        actual = b.actual_pct(total_equity)
        drift = actual - b.target_pct
        flag = ""
        target_usd = total_equity * b.target_pct / 100
        delta_usd = b.equity_usd - target_usd  # >0 = over, <0 = under
        if abs(drift) >= DRIFT_BAND_PCT and b.target_pct > 0:
            if drift > 0:
                flag = "OVER"
                rebalance_suggestions.append(
                    f"{b.name} is {drift:+.1f}% above target — "
                    f"trim ${abs(delta_usd):.2f} to LP via bridge"
                )
            else:
                flag = "UNDER"
                # Specifically suggest source from over-allocated buckets
                rebalance_suggestions.append(
                    f"{b.name} is {drift:+.1f}% below target — "
                    f"need +${abs(delta_usd):.2f} to reach target"
                )
        # ALSO surface wind-down buckets if they're still non-zero
        if b.target_pct == 0 and b.equity_usd > 5:
            rebalance_suggestions.append(
                f"{b.name} has ${b.equity_usd:.2f} idle (target 0%) — "
                f"withdraw to LP or HL"
            )
        bucket_rows.append({
            "name": b.name,
            "equity_usd": str(b.equity_usd),
            "free_usd": str(b.free_usd),
            "target_pct": str(b.target_pct),
            "actual_pct": str(actual.quantize(Decimal("0.01"))),
            "drift_pct": str(drift.quantize(Decimal("0.01"))),
            "delta_usd": str(delta_usd.quantize(Decimal("0.01"))),
            "flag": flag,
            "note": b.note,
        })

    snapshot = {
        "generated_at_iso": now.isoformat(),
        "total_equity_usd": str(total_equity),
        "total_free_usd": str(total_free),
        "drift_band_pct": str(DRIFT_BAND_PCT),
        "buckets": bucket_rows,
        "rebalance_suggestions": rebalance_suggestions,
    }
    return snapshot


def _format_telegram(snapshot: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total = Decimal(snapshot["total_equity_usd"])
    free = Decimal(snapshot["total_free_usd"])
    lines = [
        f"🏦 <b>Treasury — {now}</b>",
        f"Total equity: <code>${total:.2f}</code> · "
        f"free: <code>${free:.2f}</code>",
        "",
    ]
    for b in snapshot["buckets"]:
        flag = b["flag"]
        marker = (
            "🟢" if not flag else ("🟡" if flag == "UNDER" else "🟠")
        )
        eq = Decimal(b["equity_usd"])
        lines.append(
            f"{marker} <b>{b['name']}</b>  "
            f"<code>${eq:.2f}</code>  "
            f"({b['actual_pct']}% vs target {b['target_pct']}%, "
            f"drift {b['drift_pct']}%)"
        )
        if b["note"]:
            lines.append(f"   <i>⚠️ {b['note']}</i>")
    if snapshot["rebalance_suggestions"]:
        lines.append("")
        lines.append("<b>Rebalance suggestions:</b>")
        for s in snapshot["rebalance_suggestions"]:
            lines.append(f"• {s}")
    else:
        lines.append("")
        lines.append("✅ All buckets within ±" + str(DRIFT_BAND_PCT) + "% drift band.")
    return "\n".join(lines)


def run_once(*, fire_telegram: bool = True) -> dict:
    started_ts = time.time()
    snapshot = build_snapshot()
    snapshot["elapsed_sec"] = round(time.time() - started_ts, 2)
    LATEST_PATH.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True, default=str)
    )

    if fire_telegram:
        try:
            from engine.telegram.client import send
            text = _format_telegram(snapshot)
            # Use audit-yellow category if rebalance suggestions exist, else daily-brief
            cat = (
                "audit-yellow"
                if snapshot["rebalance_suggestions"]
                else "daily-brief"
            )
            send(
                cat,
                key=f"treasury:{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}",
                text=text,
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning(f"[treasury] telegram digest failed: {exc}",
                           exc_info=True)
    return snapshot


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Capital treasury digest")
    p.add_argument("--once", action="store_true")
    p.add_argument("--no-telegram", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    snapshot = run_once(fire_telegram=not args.no_telegram)
    print(json.dumps({
        "total_equity_usd": snapshot["total_equity_usd"],
        "total_free_usd": snapshot["total_free_usd"],
        "rebalance_count": len(snapshot["rebalance_suggestions"]),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
