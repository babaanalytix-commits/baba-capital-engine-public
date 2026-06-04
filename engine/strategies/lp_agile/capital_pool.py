"""engine/strategies/lp_agile/capital_pool.py — multi-user LP capital pool
ledger (#110).

Design constraints (per Yomi 2026-05-29 + audited):
- LEGAL: each contribution is structured as a PERSONAL LOAN to Yomi at
  variable interest = LP_returns × share × 50%. NOT a managed investment.
  Written agreement (WhatsApp screenshot per contributor) outside this
  module's scope.
- CUSTODY: the LP wallet (0x0108…8482) holds ALL capital. Contributors do
  NOT have any on-chain control. This module is bookkeeping only — never
  signs a tx.
- SHARE ACCOUNTING: high-water mark NAV-based. When a contributor adds X
  USD at NAV=N, they get X/N share. NAV growth distributes proportionally.
  Yomi's cut is 50% of contributor's notional gain on their share.
- WITHDRAWAL: contributor requests amount; Yomi approves; physical transfer
  out of wallet happens out-of-band and is recorded as a withdrawal event.
- TRANSPARENCY: contributor signed-URL view (engine/api/server.py extension)
  shows their share %, contribution history, gross fees, Yomi-cut, net
  interest, withdraw-request button.

Schema:
  contributors    (id, name, contact, signed_url_token, created_at_iso, status)
  contributions   (id, contributor_id, amount_usd, contributed_at_iso,
                   nav_at_contribution, share_pct_at_contribution, tx_hash,
                   note)
  snapshots       (id, taken_at_iso, total_nav_usd, contributor_shares_json)
  withdrawals     (id, contributor_id, requested_usd, requested_at_iso,
                   approved_at_iso, paid_at_iso, tx_hash, status, note)

NEVER raises in user-facing methods — returns None / empty list on failure.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.capital_pool")

_REPO_ROOT = Path(__file__).resolve().parents[3]
DB_PATH = _REPO_ROOT / "engine" / "_registries" / "lp_capital_pool.db"


# ──────────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────────


SCHEMA = """
CREATE TABLE IF NOT EXISTS contributors (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    contact           TEXT,
    signed_url_token  TEXT UNIQUE NOT NULL,
    created_at_iso    TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'active',  -- active | withdrawn | suspended
    -- SAFE-2.5: per-contributor high-water mark of the gross PnL on which
    -- Yomi's cut has already been crystallised. The cut is charged ONLY on
    -- gains above this mark, so a drawdown-and-recovery isn't billed twice.
    hwm_gross_pnl_usd REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS contributions (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    contributor_id                INTEGER NOT NULL REFERENCES contributors(id),
    amount_usd                    REAL NOT NULL,
    contributed_at_iso            TEXT NOT NULL,
    nav_at_contribution           REAL NOT NULL,
    share_pct_at_contribution     REAL NOT NULL,
    tx_hash                       TEXT,
    note                          TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at_iso             TEXT NOT NULL,
    total_nav_usd            REAL NOT NULL,
    contributor_shares_json  TEXT NOT NULL  -- {contributor_id: {share_pct, gross_pnl_usd, yomi_cut_usd, net_to_contrib_usd}}
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    contributor_id      INTEGER NOT NULL REFERENCES contributors(id),
    requested_usd       REAL NOT NULL,
    requested_at_iso    TEXT NOT NULL,
    approved_at_iso     TEXT,
    paid_at_iso         TEXT,
    tx_hash             TEXT,
    status              TEXT NOT NULL DEFAULT 'requested',  -- requested|approved|paid|rejected
    note                TEXT
);

CREATE INDEX IF NOT EXISTS idx_contrib_ts        ON contributions(contributed_at_iso);
CREATE INDEX IF NOT EXISTS idx_contrib_who       ON contributions(contributor_id);
CREATE INDEX IF NOT EXISTS idx_snap_ts           ON snapshots(taken_at_iso);
CREATE INDEX IF NOT EXISTS idx_withdraw_status   ON withdrawals(status);
"""


# ──────────────────────────────────────────────────────────────────────
# DB plumbing
# ──────────────────────────────────────────────────────────────────────


def _ensure_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.executescript(SCHEMA)
        # SAFE-2.5 migration: existing DBs created before the HWM column was
        # added won't get it from CREATE TABLE IF NOT EXISTS — add it idempotently.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(contributors)").fetchall()}
        if "hwm_gross_pnl_usd" not in cols:
            conn.execute("ALTER TABLE contributors ADD COLUMN "
                         "hwm_gross_pnl_usd REAL NOT NULL DEFAULT 0")
        conn.commit()


@contextmanager
def _conn():
    _ensure_db()
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_token() -> str:
    """URL-safe random token for per-contributor signed views."""
    return secrets.token_urlsafe(32)


# ──────────────────────────────────────────────────────────────────────
# Yomi's cut policy
# ──────────────────────────────────────────────────────────────────────


YOMI_CUT_FRACTION = float(os.environ.get("LP_CAPITAL_POOL_YOMI_CUT_FRAC", "0.50"))


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────


@dataclass
class Contributor:
    id: int
    name: str
    contact: Optional[str]
    signed_url_token: str
    created_at_iso: str
    status: str
    hwm_gross_pnl_usd: float = 0.0   # SAFE-2.5


def add_contributor(name: str, contact: Optional[str] = None) -> Optional[Contributor]:
    """Register a new contributor. Returns Contributor with their signed URL token."""
    try:
        token = _gen_token()
        with _conn() as c:
            cur = c.execute(
                "INSERT INTO contributors(name, contact, signed_url_token, created_at_iso) "
                "VALUES (?, ?, ?, ?)",
                (name.strip(), contact, token, _now_iso()),
            )
            c.commit()
            cid = cur.lastrowid
            row = c.execute("SELECT * FROM contributors WHERE id=?", (cid,)).fetchone()
        return Contributor(**dict(row))
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("[capital_pool] add_contributor failed: %s", exc)
        return None


def record_contribution(
    contributor_id: int, amount_usd: float, total_nav_usd_now: float,
    tx_hash: Optional[str] = None, note: Optional[str] = None,
) -> bool:
    """Record a contribution.

    NAV-based share: if pool NAV (before contribution) is N and contributor
    adds X, their share = X / (N + X). All previous contributors' shares
    dilute proportionally — we don't store absolute share, we recompute
    from contribution history + current NAV (see compute_shares()).
    """
    try:
        nav_after = float(total_nav_usd_now) + float(amount_usd)
        share_pct = (float(amount_usd) / nav_after * 100) if nav_after > 0 else 0.0
        with _conn() as c:
            c.execute(
                "INSERT INTO contributions(contributor_id, amount_usd, "
                "contributed_at_iso, nav_at_contribution, share_pct_at_contribution, "
                "tx_hash, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (contributor_id, float(amount_usd), _now_iso(),
                 float(total_nav_usd_now), share_pct, tx_hash, note),
            )
            c.commit()
        return True
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("[capital_pool] record_contribution failed: %s", exc)
        return False


def request_withdrawal(
    contributor_id: int, amount_usd: float, note: Optional[str] = None,
) -> Optional[int]:
    """Contributor requests a withdrawal. Returns withdrawal id."""
    try:
        with _conn() as c:
            cur = c.execute(
                "INSERT INTO withdrawals(contributor_id, requested_usd, "
                "requested_at_iso, status, note) VALUES (?, ?, ?, 'requested', ?)",
                (contributor_id, float(amount_usd), _now_iso(), note),
            )
            c.commit()
            return cur.lastrowid
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("[capital_pool] request_withdrawal failed: %s", exc)
        return None


def approve_withdrawal(withdrawal_id: int, tx_hash: Optional[str] = None) -> bool:
    """Operator marks withdrawal approved + records payout tx hash."""
    try:
        with _conn() as c:
            c.execute(
                "UPDATE withdrawals SET status='approved', approved_at_iso=? "
                "WHERE id=?",
                (_now_iso(), withdrawal_id),
            )
            if tx_hash:
                c.execute(
                    "UPDATE withdrawals SET status='paid', paid_at_iso=?, tx_hash=? "
                    "WHERE id=?",
                    (_now_iso(), tx_hash, withdrawal_id),
                )
            c.commit()
        return True
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("[capital_pool] approve_withdrawal failed: %s", exc)
        return False


def compute_shares(total_nav_usd_now: float) -> dict:
    """Compute each contributor's current share + accrued interest given
    the current pool NAV.

    Share is computed as (total_contributed - total_withdrawn) / Σ(contributed - withdrawn).
    Interest is the contributor's share of NAV growth above their net invested capital.
    Yomi's cut is YOMI_CUT_FRACTION × gross interest per contributor.

    Returns: {
        "total_nav_usd": float,
        "total_invested_net": float,
        "yomi_cut_total_usd": float,
        "contributors": [
            {"id", "name", "share_pct", "net_invested_usd", "gross_pnl_usd",
             "yomi_cut_usd", "net_to_contributor_usd"}
        ]
    }
    """
    try:
        with _conn() as c:
            contribs = c.execute("SELECT * FROM contributors").fetchall()
            net_invested_by_id: dict[int, float] = {}
            for row in contribs:
                rid = row["id"]
                # Sum all contributions
                contrib_sum = c.execute(
                    "SELECT COALESCE(SUM(amount_usd), 0) AS s FROM contributions "
                    "WHERE contributor_id=?", (rid,),
                ).fetchone()["s"]
                # Sum all paid withdrawals
                paid_sum = c.execute(
                    "SELECT COALESCE(SUM(requested_usd), 0) AS s FROM withdrawals "
                    "WHERE contributor_id=? AND status='paid'", (rid,),
                ).fetchone()["s"]
                net_invested_by_id[rid] = float(contrib_sum) - float(paid_sum)

        total_invested = sum(v for v in net_invested_by_id.values() if v > 0)
        # Pool growth distributed proportionally on net-invested
        growth_total = float(total_nav_usd_now) - total_invested
        if total_invested <= 0:
            growth_total = 0.0

        result_contribs = []
        yomi_cut_total = 0.0
        for row in contribs:
            rid = row["id"]
            net_inv = net_invested_by_id[rid]
            if total_invested > 0 and net_inv > 0:
                share = net_inv / total_invested
            else:
                share = 0.0
            gross_pnl = growth_total * share
            # SAFE-2.5 high-water mark: the cut applies ONLY to gains above the
            # contributor's prior crystallised high. Below the HWM (i.e. still
            # recovering a past drawdown) the fee basis is zero, so the same
            # gain is never charged twice. Crystallise the mark via crystallize_hwm().
            hwm = float(row["hwm_gross_pnl_usd"] or 0.0)
            fee_basis = max(gross_pnl - hwm, 0.0)
            yomi_cut = fee_basis * YOMI_CUT_FRACTION
            net_to_contrib = gross_pnl - yomi_cut
            yomi_cut_total += yomi_cut
            result_contribs.append({
                "id": rid,
                "name": row["name"],
                "share_pct": round(share * 100, 4),
                "net_invested_usd": round(net_inv, 2),
                "gross_pnl_usd": round(gross_pnl, 2),
                "hwm_gross_pnl_usd": round(hwm, 2),
                "fee_basis_usd": round(fee_basis, 2),
                "yomi_cut_usd": round(yomi_cut, 2),
                "net_to_contributor_usd": round(net_to_contrib, 2),
            })

        return {
            "total_nav_usd": round(float(total_nav_usd_now), 2),
            "total_invested_net": round(total_invested, 2),
            "growth_total_usd": round(growth_total, 2),
            "yomi_cut_total_usd": round(yomi_cut_total, 2),
            "yomi_cut_fraction": YOMI_CUT_FRACTION,
            "n_contributors": len(result_contribs),
            "contributors": result_contribs,
        }
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("[capital_pool] compute_shares failed: %s", exc)
        return {"error": str(exc)[:200]}


def crystallize_hwm(total_nav_usd_now: float) -> dict:
    """Lock in the high-water mark (SAFE-2.5). Raise each contributor's
    hwm_gross_pnl_usd to their CURRENT gross PnL whenever it's a new high.

    Call this ONLY when Yomi's cut is actually realised/taken (e.g. at a
    profit withdrawal or an agreed period-end crystallisation) — NOT on every
    read/snapshot, or you'd re-introduce the double-charge this guards against.
    Never lowers a HWM (a drawdown doesn't reset the mark). Returns a summary.
    """
    try:
        breakdown = compute_shares(total_nav_usd_now)
        if breakdown.get("error"):
            return breakdown
        raised = []
        with _conn() as c:
            for cb in breakdown.get("contributors", []):
                gross = float(cb["gross_pnl_usd"])
                prev = float(cb["hwm_gross_pnl_usd"])
                if gross > prev:
                    c.execute(
                        "UPDATE contributors SET hwm_gross_pnl_usd=? WHERE id=?",
                        (gross, cb["id"]),
                    )
                    raised.append({"id": cb["id"], "name": cb["name"],
                                   "from": round(prev, 2), "to": round(gross, 2)})
            c.commit()
        logger.info("[capital_pool] crystallised HWM for %d contributor(s)", len(raised))
        return {"crystallized_at_iso": _now_iso(), "raised": raised,
                "n_raised": len(raised)}
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("[capital_pool] crystallize_hwm failed: %s", exc)
        return {"error": str(exc)[:200]}


def take_snapshot(total_nav_usd_now: float) -> Optional[int]:
    """Persist a NAV + share snapshot. Useful for time-series PnL analysis."""
    try:
        breakdown = compute_shares(total_nav_usd_now)
        with _conn() as c:
            cur = c.execute(
                "INSERT INTO snapshots(taken_at_iso, total_nav_usd, "
                "contributor_shares_json) VALUES (?, ?, ?)",
                (_now_iso(), float(total_nav_usd_now), json.dumps(breakdown, default=str)),
            )
            c.commit()
            return cur.lastrowid
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("[capital_pool] take_snapshot failed: %s", exc)
        return None


def get_contributor_by_token(token: str) -> Optional[dict]:
    """Per-user PWA view backend. Returns contributor's view payload."""
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT * FROM contributors WHERE signed_url_token=?", (token,)
            ).fetchone()
            if not row:
                return None
            return dict(row)
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("[capital_pool] get_contributor_by_token failed: %s", exc)
        return None


def list_pending_withdrawals() -> list[dict]:
    """Operator view: pending withdrawals requiring approval."""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT w.*, c.name AS contributor_name FROM withdrawals w "
                "JOIN contributors c ON c.id=w.contributor_id "
                "WHERE w.status='requested' ORDER BY w.requested_at_iso ASC"
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("[capital_pool] list_pending_withdrawals failed: %s", exc)
        return []


def all_contributors() -> list[dict]:
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT id, name, contact, created_at_iso, status FROM contributors "
                "ORDER BY created_at_iso ASC"
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("[capital_pool] all_contributors failed: %s", exc)
        return []


if __name__ == "__main__":
    # Smoke test
    logging.basicConfig(level=logging.INFO)
    _ensure_db()
    print("DB at", DB_PATH)
    print("contributors:", all_contributors())
    print("pending withdrawals:", list_pending_withdrawals())
