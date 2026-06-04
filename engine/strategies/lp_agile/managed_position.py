"""engine/strategies/lp_agile/managed_position.py — one-NFT-per-pair LP model.

LP architecture pivot 2026-05-30 (Yomi):
    Instead of treating each rebalance/capital change as a new position, hold
    ONE logical position per (pool, fee_tier) and mutate it over its lifetime.
    Range adjusts go through prjx execute() / Slipstream's equivalent —
    cleaner reporting, lower gas, lifetime metrics roll up naturally.

Constraint upfront: standard Uniswap V3 NFTs (incl. Slipstream + prjx) have
IMMUTABLE ticks per NFT. So when ticks change, the underlying NFT changes
tokenId (burn old + mint new, even if bundled into one tx). What stays
constant is the LOGICAL position. We track it via `current_nft_token_id`
which mutates on each rebalance.

Tables:
    managed_positions    — one row per (chain, pool_address, fee_tier)
    mutations            — append-only history of execute/increase/decrease/collect

Lifetime metrics live on managed_positions and accumulate across NFTs:
    lifetime_gas_usd
    lifetime_fees_collected_usd
    lifetime_il_realized_usd (when known)
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.managed_position")

_REPO_ROOT = Path(__file__).resolve().parents[3]
DB_PATH = _REPO_ROOT / "engine" / "_registries" / "lp_managed_positions.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ════════════════════════════════════════════════════════════════════════
# SCHEMA
# ════════════════════════════════════════════════════════════════════════

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS managed_positions (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    chain                       TEXT NOT NULL,         -- 'hyperevm', 'base'
    protocol                    TEXT NOT NULL,         -- 'prjx', 'aerodrome_slipstream', 'uniswap_v3'
    pool_address                TEXT NOT NULL,         -- lowercase 0x…
    token0_address              TEXT NOT NULL,
    token1_address              TEXT NOT NULL,
    token0_symbol               TEXT,
    token1_symbol               TEXT,
    fee_tier                    INTEGER NOT NULL,      -- 100, 500, 3000, 10000
    current_nft_token_id        INTEGER,               -- NULL before first mint
    current_tick_lower          INTEGER,
    current_tick_upper          INTEGER,
    current_liquidity           TEXT,                  -- uint128 stored as string
    opened_at_iso               TEXT NOT NULL,
    last_mutated_at_iso         TEXT,
    closed_at_iso               TEXT,
    status                      TEXT NOT NULL DEFAULT 'open',  -- open | closed | error
    -- Lifetime metrics (accumulate across NFT rotations)
    lifetime_gas_usd            REAL NOT NULL DEFAULT 0,
    lifetime_fees_collected_usd REAL NOT NULL DEFAULT 0,
    lifetime_il_realized_usd    REAL,                  -- NULL until close
    lifetime_capital_in_usd     REAL NOT NULL DEFAULT 0,
    lifetime_capital_out_usd    REAL NOT NULL DEFAULT 0,
    -- Annualised realised return on average capital invested. NULL until
    -- recompute_realized_apr() has enough lifetime to compute (≥1 day +
    -- non-zero avg capital). Read by capital_optimizer.
    realized_apr_pct            REAL,
    notes                       TEXT,
    UNIQUE (chain, pool_address, fee_tier)
);

CREATE INDEX IF NOT EXISTS idx_managed_status ON managed_positions(status);
CREATE INDEX IF NOT EXISTS idx_managed_chain  ON managed_positions(chain);

CREATE TABLE IF NOT EXISTS mutations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    managed_position_id INTEGER NOT NULL,
    mutated_at_iso      TEXT NOT NULL,
    action              TEXT NOT NULL,          -- mint | range_adjust | increase_liquidity | decrease_liquidity | collect | burn
    -- Snapshot before/after
    before_nft_token_id INTEGER,
    after_nft_token_id  INTEGER,
    before_tick_lower   INTEGER,
    before_tick_upper   INTEGER,
    after_tick_lower    INTEGER,
    after_tick_upper    INTEGER,
    -- Tx info
    tx_hash             TEXT,
    gas_used_wei        INTEGER,
    gas_price_wei       INTEGER,
    gas_cost_usd        REAL,
    -- Economic
    amount0_delta       TEXT,                   -- signed int as string ("+amt" or "-amt")
    amount1_delta       TEXT,
    fees0_collected     TEXT,
    fees1_collected     TEXT,
    fees_usd_total      REAL,
    capital_delta_usd   REAL,                   -- net USD into/out of position
    -- Audit
    triggered_by        TEXT,                   -- 'manual', 'auto_drift', 'auto_compound', 'withdraw'
    notes               TEXT,
    FOREIGN KEY (managed_position_id) REFERENCES managed_positions(id)
);

CREATE INDEX IF NOT EXISTS idx_mutations_pos    ON mutations(managed_position_id);
CREATE INDEX IF NOT EXISTS idx_mutations_action ON mutations(action);
CREATE INDEX IF NOT EXISTS idx_mutations_ts     ON mutations(mutated_at_iso);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    # Legacy-DB migration: realized_apr_pct shipped 2026-05-30 after the
    # initial table existed without it. ALTER TABLE … ADD COLUMN is idempotent
    # only behind a column-exists check.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(managed_positions)")}
    if "realized_apr_pct" not in cols:
        try:
            conn.execute(
                "ALTER TABLE managed_positions ADD COLUMN realized_apr_pct REAL"
            )
            conn.commit()
        except sqlite3.OperationalError:
            # race: another connection added it between PRAGMA and ALTER
            pass
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ════════════════════════════════════════════════════════════════════════
# WRITE API
# ════════════════════════════════════════════════════════════════════════

def upsert_managed_position(*, chain: str, protocol: str,
                             pool_address: str, fee_tier: int,
                             token0_address: str, token1_address: str,
                             token0_symbol: Optional[str] = None,
                             token1_symbol: Optional[str] = None,
                             current_nft_token_id: Optional[int] = None,
                             current_tick_lower: Optional[int] = None,
                             current_tick_upper: Optional[int] = None,
                             current_liquidity: Optional[str] = None,
                             ) -> int:
    """Create or update a managed position for the (chain, pool, fee_tier).
    Returns the managed_position id."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id FROM managed_positions WHERE chain=? AND pool_address=? AND fee_tier=?",
            (chain, pool_address.lower(), int(fee_tier))
        ).fetchone()
        if row:
            conn.execute(
                """UPDATE managed_positions SET
                    current_nft_token_id=COALESCE(?, current_nft_token_id),
                    current_tick_lower=COALESCE(?, current_tick_lower),
                    current_tick_upper=COALESCE(?, current_tick_upper),
                    current_liquidity=COALESCE(?, current_liquidity),
                    last_mutated_at_iso=?,
                    token0_symbol=COALESCE(?, token0_symbol),
                    token1_symbol=COALESCE(?, token1_symbol)
                   WHERE id=?""",
                (current_nft_token_id, current_tick_lower, current_tick_upper,
                 current_liquidity, _now_iso(),
                 token0_symbol, token1_symbol, row["id"]),
            )
            conn.commit()
            return int(row["id"])
        cur = conn.execute(
            """INSERT INTO managed_positions
                 (chain, protocol, pool_address, fee_tier,
                  token0_address, token1_address,
                  token0_symbol, token1_symbol,
                  current_nft_token_id,
                  current_tick_lower, current_tick_upper, current_liquidity,
                  opened_at_iso, last_mutated_at_iso, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
            (chain, protocol, pool_address.lower(), int(fee_tier),
             token0_address.lower(), token1_address.lower(),
             token0_symbol, token1_symbol,
             current_nft_token_id,
             current_tick_lower, current_tick_upper, current_liquidity,
             _now_iso(), _now_iso()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def record_mutation(*, managed_position_id: int, action: str,
                     before_nft_token_id: Optional[int] = None,
                     after_nft_token_id: Optional[int] = None,
                     before_tick_lower: Optional[int] = None,
                     before_tick_upper: Optional[int] = None,
                     after_tick_lower: Optional[int] = None,
                     after_tick_upper: Optional[int] = None,
                     tx_hash: Optional[str] = None,
                     gas_used_wei: Optional[int] = None,
                     gas_price_wei: Optional[int] = None,
                     gas_cost_usd: Optional[float] = None,
                     amount0_delta: Optional[str] = None,
                     amount1_delta: Optional[str] = None,
                     fees0_collected: Optional[str] = None,
                     fees1_collected: Optional[str] = None,
                     fees_usd_total: Optional[float] = None,
                     capital_delta_usd: Optional[float] = None,
                     triggered_by: Optional[str] = None,
                     notes: Optional[str] = None,
                     ) -> int:
    """Append a mutation row + update aggregate lifetime metrics on the
    parent managed position. Returns mutation id."""
    if action not in (
        "mint", "range_adjust", "increase_liquidity", "decrease_liquidity",
        "collect", "burn"
    ):
        raise ValueError(f"unknown mutation action: {action}")
    conn = _conn()
    try:
        cur = conn.execute(
            """INSERT INTO mutations
                (managed_position_id, mutated_at_iso, action,
                 before_nft_token_id, after_nft_token_id,
                 before_tick_lower, before_tick_upper,
                 after_tick_lower, after_tick_upper,
                 tx_hash, gas_used_wei, gas_price_wei, gas_cost_usd,
                 amount0_delta, amount1_delta,
                 fees0_collected, fees1_collected, fees_usd_total,
                 capital_delta_usd, triggered_by, notes)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (managed_position_id, _now_iso(), action,
             before_nft_token_id, after_nft_token_id,
             before_tick_lower, before_tick_upper,
             after_tick_lower, after_tick_upper,
             tx_hash, gas_used_wei, gas_price_wei, gas_cost_usd,
             amount0_delta, amount1_delta,
             fees0_collected, fees1_collected, fees_usd_total,
             capital_delta_usd, triggered_by, notes),
        )
        mid = int(cur.lastrowid)
        # Roll up aggregates
        updates = []
        params: list = []
        if gas_cost_usd is not None:
            updates.append("lifetime_gas_usd = lifetime_gas_usd + ?")
            params.append(float(gas_cost_usd))
        if fees_usd_total is not None:
            updates.append(
                "lifetime_fees_collected_usd = lifetime_fees_collected_usd + ?"
            )
            params.append(float(fees_usd_total))
        if capital_delta_usd is not None:
            if capital_delta_usd > 0:
                updates.append(
                    "lifetime_capital_in_usd = lifetime_capital_in_usd + ?"
                )
                params.append(float(capital_delta_usd))
            else:
                updates.append(
                    "lifetime_capital_out_usd = lifetime_capital_out_usd + ?"
                )
                params.append(float(-capital_delta_usd))
        # Update current ticks/NFT if after_* provided
        if after_nft_token_id is not None:
            updates.append("current_nft_token_id = ?")
            params.append(after_nft_token_id)
        if after_tick_lower is not None:
            updates.append("current_tick_lower = ?")
            params.append(after_tick_lower)
        if after_tick_upper is not None:
            updates.append("current_tick_upper = ?")
            params.append(after_tick_upper)
        updates.append("last_mutated_at_iso = ?")
        params.append(_now_iso())
        params.append(managed_position_id)
        conn.execute(
            f"UPDATE managed_positions SET {', '.join(updates)} WHERE id=?",
            params,
        )
        conn.commit()
        return mid
    finally:
        conn.close()


def _parse_delta(s) -> float:
    """Parse a signed token-delta string ('+1.5' / '-0.9' / '1.5') → float."""
    if s is None or s == "":
        return 0.0
    try:
        return float(str(s))
    except (TypeError, ValueError):
        return 0.0


_USD_QUOTE_SYMBOLS = {"USDC", "USDT", "USD", "DAI", "USDC.E", "USDBC"}


def realized_il_for_managed(managed_position_id: int, *,
                            exit_price0_usd: float) -> Optional[dict]:
    """Reconstruct entry vs exit token baskets from the mutation log and compute
    REALIZED impermanent loss in USD (LP-2, 2026-05-30).

    IL = (entry basket valued at exit) − (what the position actually returned).
    Model-free: uses on-chain token deltas, not an approximation. Only computed
    for USD-quoted pairs (token1 a stablecoin); returns None otherwise so a
    HYPE/BTC-style pair never gets a mis-denominated number.
    """
    pos = get_managed_position(managed_position_id)
    if not pos:
        return None
    if (pos.get("token1_symbol") or "").upper() not in _USD_QUOTE_SYMBOLS:
        return None
    muts = list_mutations(managed_position_id=managed_position_id, limit=100000)
    entry0 = entry1 = exit0 = exit1 = 0.0
    for m in muts:
        a0, a1 = _parse_delta(m.get("amount0_delta")), _parse_delta(m.get("amount1_delta"))
        if m["action"] in ("mint", "increase_liquidity"):
            entry0 += max(a0, 0.0); entry1 += max(a1, 0.0)
        elif m["action"] in ("decrease_liquidity", "burn"):
            exit0 += max(-a0, 0.0); exit1 += max(-a1, 0.0)
    if entry0 == 0.0 and entry1 == 0.0:
        return None
    hodl = entry0 * exit_price0_usd + entry1
    lp = exit0 * exit_price0_usd + exit1
    return {"il_usd": hodl - lp, "hodl_value_usd": hodl, "lp_value_usd": lp,
            "entry_token0": entry0, "entry_token1": entry1,
            "exit_token0": exit0, "exit_token1": exit1}


def close_managed_position(managed_position_id: int, *,
                            il_realized_usd: Optional[float] = None,
                            exit_price0_usd: Optional[float] = None,
                            note: Optional[str] = None) -> None:
    # LP-2: if IL wasn't passed explicitly but we have the exit price, compute
    # realised IL from the mutation log so lifetime_il_realized_usd is populated
    # (best-effort — never blocks the close).
    if il_realized_usd is None and exit_price0_usd is not None:
        try:
            r = realized_il_for_managed(managed_position_id, exit_price0_usd=exit_price0_usd)
            if r is not None:
                il_realized_usd = float(r["il_usd"])
        except Exception as exc:                              # noqa: BLE001
            logger.debug("[managed_position] IL auto-compute skipped: %s", exc)
    conn = _conn()
    try:
        conn.execute(
            """UPDATE managed_positions SET
                status='closed',
                closed_at_iso=?,
                lifetime_il_realized_usd=COALESCE(?, lifetime_il_realized_usd),
                notes=COALESCE(?, notes)
              WHERE id=?""",
            (_now_iso(), il_realized_usd, note, managed_position_id),
        )
        conn.commit()
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════════
# READ API
# ════════════════════════════════════════════════════════════════════════

def list_managed_positions(*, status: Optional[str] = None,
                            chain: Optional[str] = None) -> list[dict]:
    conn = _conn()
    try:
        where = []
        params = []
        if status:
            where.append("status = ?")
            params.append(status)
        if chain:
            where.append("chain = ?")
            params.append(chain)
        sql = "SELECT * FROM managed_positions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY opened_at_iso DESC"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def get_managed_position(managed_position_id: int) -> Optional[dict]:
    conn = _conn()
    try:
        r = conn.execute(
            "SELECT * FROM managed_positions WHERE id=?",
            (managed_position_id,),
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def find_by_pool(*, chain: str, pool_address: str,
                  fee_tier: int) -> Optional[dict]:
    conn = _conn()
    try:
        r = conn.execute(
            """SELECT * FROM managed_positions
               WHERE chain=? AND pool_address=? AND fee_tier=?""",
            (chain, pool_address.lower(), int(fee_tier)),
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def list_mutations(*, managed_position_id: int,
                    limit: int = 50) -> list[dict]:
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(
            """SELECT * FROM mutations
               WHERE managed_position_id=?
               ORDER BY id DESC LIMIT ?""",
            (managed_position_id, int(limit)),
        ).fetchall()]
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════════
# BACKFILL — populate from engine/_reports/lp_positions_latest.json
# ════════════════════════════════════════════════════════════════════════

_PROTOCOL_TO_CHAIN = {
    "prjx": "hyperevm",
    "hyperswap": "hyperevm",
    "slipstream": "base",
    "aerodrome": "base",
    "aerodrome_slipstream": "base",
    "uniswap_v3": "base",  # default; can be overridden by explicit p['chain']
    "uniswap_v4": "base",
}

# Default fee tier per protocol (most common). Used when snapshot omits
# fee_tier — we'll backfill the real value once a fee-tier scan ships.
_DEFAULT_FEE_TIER = {
    "prjx": 3000,
    "hyperswap": 3000,
    "slipstream": 100,
    "aerodrome": 3000,
    "aerodrome_slipstream": 100,
    "uniswap_v3": 3000,
}


def _normalise_pair(pair: str) -> tuple[Optional[str], Optional[str]]:
    """'WHYPE/USDT0' → ('WHYPE', 'USDT0')."""
    if not pair or "/" not in pair:
        return None, None
    a, b = pair.split("/", 1)
    return a.strip() or None, b.strip() or None


def backfill_from_positions_json(*, dry_run: bool = False) -> dict:
    """Read the LP snapshot the scanner publishes (lp_agile_latest.json) and
    upsert one managed_positions row per unique (chain, pool, fee_tier).
    Idempotent."""
    src = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_agile_latest.json"
    summary = {"source": str(src), "source_exists": src.exists(),
               "positions_seen": 0, "managed_upserts": 0,
               "skipped_no_pool": 0, "dry_run": dry_run}
    if not src.exists():
        return summary
    try:
        data = json.loads(src.read_text())
    except Exception as exc:
        summary["error"] = f"parse failed: {exc}"
        return summary
    positions = (data.get("open_positions")
                 or data.get("positions")
                 or data.get("ranked_pools")
                 or [])
    if not isinstance(positions, list):
        summary["error"] = "positions list missing"
        return summary
    summary["positions_seen"] = len(positions)
    seen_keys: set = set()
    for p in positions:
        protocol = (p.get("protocol") or "").lower() or "uniswap_v3"
        chain = (p.get("chain") or "").lower() or _PROTOCOL_TO_CHAIN.get(
            protocol, "base"
        )
        pool_addr = (p.get("pool_address") or p.get("pool_id") or "").lower()
        fee_tier = int(
            p.get("fee_tier") or p.get("fee")
            or _DEFAULT_FEE_TIER.get(protocol, 3000)
        )
        token0_sym, token1_sym = _normalise_pair(p.get("pair") or "")
        # token0/token1 addresses optional in this snapshot — synthesise
        # placeholders so the UNIQUE constraint still holds per pool.
        token0 = (p.get("token0_address") or
                  f"0x{token0_sym or 'tok0':>040s}").lower()
        token1 = (p.get("token1_address") or
                  f"0x{token1_sym or 'tok1':>040s}").lower()
        if not pool_addr:
            summary["skipped_no_pool"] += 1
            continue
        # Dedupe within this run — multiple open NFTs in the same pool
        # should collapse to one managed position.
        key = (chain, pool_addr, fee_tier)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if dry_run:
            summary["managed_upserts"] += 1
            continue
        upsert_managed_position(
            chain=chain, protocol=protocol,
            pool_address=pool_addr, fee_tier=fee_tier,
            token0_address=token0, token1_address=token1,
            token0_symbol=token0_sym, token1_symbol=token1_sym,
            current_nft_token_id=(
                int(p["nft_token_id"]) if p.get("nft_token_id") else None
            ),
            current_tick_lower=p.get("tick_lower"),
            current_tick_upper=p.get("tick_upper"),
            current_liquidity=(
                str(p["liquidity"]) if p.get("liquidity") is not None else None
            ),
        )
        summary["managed_upserts"] += 1
    return summary


# ════════════════════════════════════════════════════════════════════════
# REALISED APR (#171)
# ════════════════════════════════════════════════════════════════════════
#
# Formula (Yomi spec 2026-05-30):
#   realized_apr_pct = (lifetime_fees_collected_usd - lifetime_gas_usd)
#                      / avg_capital_invested
#                      * (365.25 / days_open) * 100
#
# avg_capital_invested = (lifetime_capital_in_usd - lifetime_capital_out_usd)
#                        averaged over the lifetime. For now we use the
#                        net-still-in (in − out) as the standing approximation
#                        since we don't yet keep a time-weighted capital
#                        ledger. When capital ledger ships, switch to a true
#                        time-weighted mean. Today's denominator is at least
#                        directionally correct.
#
# Edge cases:
#   days_open < 1                       → NULL (preserved current behavior)
#   avg_capital_invested == 0           → NULL
#   Any parse/compute exception         → NULL (silent — never raise)


def _safe_parse_iso(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None


def recompute_realized_apr(
    managed_position_id: int,
    *,
    current_value_usd_override: Optional[float] = None,
) -> Optional[float]:
    """Compute and persist realized_apr_pct for one managed position.

    Returns the computed APR (float, percent) or None when not enough data.

    #205 (2026-05-31): added current_value_usd fallback. When
    lifetime_capital_in_usd is 0 (no mutations recorded yet) we still want
    a non-NULL APR for the PWA. Fallback chain:
      1. avg_capital = cap_in - cap_out   (canonical, requires mutations)
      2. avg_capital = current_value_usd_override (live chain-derived value)
      3. NULL — give up, no data
    Caller can pass current_value_usd_override from a live chain read so the
    formula always has a denominator.
    """
    conn = _conn()
    try:
        row = conn.execute(
            """SELECT lifetime_fees_collected_usd, lifetime_gas_usd,
                      lifetime_capital_in_usd, lifetime_capital_out_usd,
                      opened_at_iso
               FROM managed_positions WHERE id=?""",
            (managed_position_id,),
        ).fetchone()
        if not row:
            return None
        opened = _safe_parse_iso(row["opened_at_iso"])
        if not opened:
            conn.execute(
                "UPDATE managed_positions SET realized_apr_pct=NULL WHERE id=?",
                (managed_position_id,),
            )
            conn.commit()
            return None
        days_open = (datetime.now(timezone.utc) - opened).total_seconds() / 86400.0
        if days_open < 1:
            conn.execute(
                "UPDATE managed_positions SET realized_apr_pct=NULL WHERE id=?",
                (managed_position_id,),
            )
            conn.commit()
            return None
        cap_in = float(row["lifetime_capital_in_usd"] or 0)
        cap_out = float(row["lifetime_capital_out_usd"] or 0)
        avg_capital = cap_in - cap_out
        if avg_capital <= 0:
            # #205 fallback: use current live-value as the denominator. This
            # is an approximation — true avg-capital would be time-weighted
            # over the position lifetime — but it's strictly better than NULL
            # and means the PWA always shows an APR for a position with fees.
            if current_value_usd_override and current_value_usd_override > 0:
                avg_capital = float(current_value_usd_override)
            else:
                conn.execute(
                    "UPDATE managed_positions SET realized_apr_pct=NULL WHERE id=?",
                    (managed_position_id,),
                )
                conn.commit()
                return None
        fees = float(row["lifetime_fees_collected_usd"] or 0)
        gas = float(row["lifetime_gas_usd"] or 0)
        net = fees - gas
        try:
            apr = (net / avg_capital) * (365.25 / days_open) * 100.0
        except Exception:
            apr = None
        conn.execute(
            "UPDATE managed_positions SET realized_apr_pct=? WHERE id=?",
            (apr, managed_position_id),
        )
        conn.commit()
        return apr
    finally:
        conn.close()


def set_lifetime_capital_in_usd(managed_position_id: int, value_usd: float) -> bool:
    """#205 backfill helper. Sets lifetime_capital_in_usd directly when
    deposit history isn't in the mutations table. Safe to call repeatedly:
    sets the value rather than incrementing.

    Returns True if a row was updated, False if no such id.
    """
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE managed_positions SET lifetime_capital_in_usd=? WHERE id=?",
            (float(value_usd), managed_position_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _read_lp_latest_value_by_nft() -> dict:
    """Read engine/_signals/lp_agile_latest.json (or pwa/serve mirror) and
    return {nft_token_id_str → current_value_usd_float}. Best-effort: returns
    {} on any failure so callers can fall back gracefully. (#205)
    """
    import json as _json
    from pathlib import Path as _P
    repo = _P(__file__).resolve().parents[3]
    candidates = [
        repo / "engine" / "_signals" / "lp_agile_latest.json",
        repo / "ops" / "pwa" / "serve" / "lp_agile_latest.json",
        repo / "engine" / "_signals" / "lp_wallet_positions_latest.json",
    ]
    out: dict = {}
    for path in candidates:
        try:
            if not path.exists():
                continue
            d = _json.loads(path.read_text())
            # multiple shapes — be lenient
            positions = (d.get("open_positions") or d.get("positions")
                         or d.get("staked_positions") or [])
            for pos in positions:
                token_id = (pos.get("token_id") or pos.get("nft_token_id")
                            or pos.get("current_nft_token_id"))
                val = (pos.get("position_value_usd")
                       or pos.get("value_usd")
                       or pos.get("current_value_usd"))
                if token_id and val:
                    try:
                        out[str(token_id)] = float(val)
                    except Exception:
                        pass
            if out:
                return out
        except Exception:
            continue
    return out


def recompute_all_realized_apr(
    *,
    use_live_value_fallback: bool = True,
    backfill_cap_in: bool = False,
) -> dict:
    """Walk every position (any status) and recompute realized_apr_pct.

    Returns {n_total, n_populated, n_null, per_id: [...]}. Safe to run from
    cron or the daily digest — read-only on lifetime metrics, only writes
    the realized_apr_pct column.

    #205 (2026-05-31):
      use_live_value_fallback (default True) — when a position has
        lifetime_capital_in_usd=0, look up its NFT in lp_agile_latest.json
        and use position_value_usd as the avg-capital denominator.
      backfill_cap_in (default False) — additionally PERSIST the
        live-derived value into lifetime_capital_in_usd so future runs
        don't need the override. Safe to do once per position.
    """
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, current_nft_token_id, lifetime_capital_in_usd "
            "FROM managed_positions ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    live_values = _read_lp_latest_value_by_nft() if use_live_value_fallback else {}
    per_id = []
    n_pop = 0
    n_null = 0
    n_backfilled = 0
    for row in rows:
        pid = row["id"]
        nft = str(row["current_nft_token_id"] or "")
        cap_in = float(row["lifetime_capital_in_usd"] or 0)
        override = None
        if cap_in <= 0 and nft and nft in live_values:
            override = live_values[nft]
            if backfill_cap_in and override and override > 0:
                set_lifetime_capital_in_usd(pid, override)
                n_backfilled += 1
        apr = recompute_realized_apr(pid, current_value_usd_override=override)
        per_id.append({
            "id": pid,
            "realized_apr_pct": apr,
            "live_value_used": override,
        })
        if apr is None:
            n_null += 1
        else:
            n_pop += 1
    return {
        "n_total": len(rows),
        "n_populated": n_pop,
        "n_null": n_null,
        "n_backfilled_cap_in": n_backfilled,
        "per_id": per_id,
    }


def hard_delete_managed_position(managed_position_id: int) -> int:
    """Delete a row + its mutation history. Use only for cleanup of test
    data — production close should use close_managed_position() instead."""
    conn = _conn()
    try:
        n = conn.execute("DELETE FROM mutations WHERE managed_position_id=?",
                         (managed_position_id,)).rowcount
        conn.execute("DELETE FROM managed_positions WHERE id=?",
                     (managed_position_id,))
        conn.commit()
        return n
    finally:
        conn.close()


def clear_all_positions() -> dict:
    """Nuke every row + mutation. For 'reset and re-backfill' workflows."""
    conn = _conn()
    try:
        n_mut = conn.execute("DELETE FROM mutations").rowcount
        n_pos = conn.execute("DELETE FROM managed_positions").rowcount
        conn.commit()
        return {"deleted_mutations": n_mut, "deleted_positions": n_pos}
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(prog="managed_position")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="Create schema (idempotent)")
    sub.add_parser("list", help="List managed positions")
    p_back = sub.add_parser("backfill",
                            help="Backfill from lp_agile_latest.json")
    p_back.add_argument("--dry-run", action="store_true")
    p_del = sub.add_parser("delete", help="Hard-delete one managed position by id")
    p_del.add_argument("id", type=int)
    sub.add_parser("clear-all",
                   help="Nuke every row + mutation (use for clean re-backfill)")
    p_apr = sub.add_parser(
        "recompute-apr",
        help="Recompute realized_apr_pct on every managed position")
    p_apr.add_argument(
        "--backfill-cap-in", action="store_true",
        help="Also persist live position_value_usd into "
             "lifetime_capital_in_usd when it is 0 (#205 backfill).")
    p_apr.add_argument(
        "--no-live-fallback", action="store_true",
        help="Disable live-value fallback (strict mutations-only mode).")
    p_set = sub.add_parser(
        "set-cap-in",
        help="Manually set lifetime_capital_in_usd for one position.")
    p_set.add_argument("id", type=int)
    p_set.add_argument("value_usd", type=float)
    args = ap.parse_args()
    if args.cmd == "init":
        _conn().close()
        print(f"schema initialised at {DB_PATH}")
    elif args.cmd == "list":
        rows = list_managed_positions()
        print(f"=== {len(rows)} managed positions ===")
        for r in rows:
            print(f"  [{r['id']:3d}] {r['chain']:8s} {r['protocol']:22s} "
                  f"{r['token0_symbol'] or '?':6s}/{r['token1_symbol'] or '?':6s} "
                  f"fee={r['fee_tier']:>5d}  "
                  f"nft={r['current_nft_token_id']}  "
                  f"ticks=[{r['current_tick_lower']},{r['current_tick_upper']}]  "
                  f"status={r['status']}")
    elif args.cmd == "backfill":
        s = backfill_from_positions_json(dry_run=args.dry_run)
        import json as _j
        print(_j.dumps(s, indent=2))
    elif args.cmd == "delete":
        n = hard_delete_managed_position(args.id)
        print(f"deleted position id={args.id} (+ {n} mutation rows)")
    elif args.cmd == "clear-all":
        print(clear_all_positions())
    elif args.cmd == "recompute-apr":
        s = recompute_all_realized_apr(
            use_live_value_fallback=not args.no_live_fallback,
            backfill_cap_in=args.backfill_cap_in,
        )
        import json as _j
        print(_j.dumps(s, indent=2))
    elif args.cmd == "set-cap-in":
        ok = set_lifetime_capital_in_usd(args.id, args.value_usd)
        print(f"set lifetime_capital_in_usd={args.value_usd} on id={args.id}: "
              f"{'OK' if ok else 'NO SUCH ROW'}")
