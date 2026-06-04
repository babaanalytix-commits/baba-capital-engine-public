"""engine/strategies/lp_agile/idle_deploy_picker.py — multi-pool target picker.

The single source of truth for "which managed_position should the next
idle-deploy cycle top up?". Used by BOTH the Aerodrome and prjx
dispatchers so they share the same fill policy.

POLICY (Yomi 2026-06-04 #420):
  Goal: build to $2K/mo passive income → ~$50-80K LP at 30-50% APR.
  Strategy: concentrate before diversifying. Per-chain floor (default $500)
  defines the minimum capital we want in any active pool. Until a pool is
  at floor, ALL fresh capital from idle-deploy goes there. Above floor,
  capital flows to highest projected APR.

RULE (Yomi 2026-06-04 #420 override):
  HIGHEST APR WINS. ALWAYS. It's all about return on investment.

  Among all open managed positions on the chain, pick the one with the
  highest projected APR (sourced first from DeFiLlama leaderboard, then
  from chain snapshot, then from registry).

  Tie-breaker: larger current_value_usd (concentrate over fragment).

  The $500 floor still exists but it's used elsewhere (planner: should we
  open a NEW pool yet?). It is NOT used here for picking targets among
  existing pools — APR rules.

Inputs (all read-only):
  - managed_positions registry (filter chain + open + has nft_token_id)
  - chain snapshot (ops/pwa/serve/lp_agile_latest.json) for live value_usd
  - DeFiLlama lookup_apr for projected APR
  - per-chain floor (env LP_PER_POOL_FLOOR_USD_{CHAIN} or default 500)

Output: dict {"nft_token_id", "id", "pair", "current_value_usd",
              "projected_apr_pct", "reason", "below_floor", "fill_gap_usd"}.
Returns None if no eligible position.

Standing directives:
  - Never churn live positions to hit a count [[lp-allocation-planner]]
  - Pure function — easy to unit test (chain snapshot + APR map are inputs)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.idle_deploy_picker")

_REPO = Path(__file__).resolve().parents[3]
MANAGED_DB = _REPO / "engine" / "_registries" / "lp_managed_positions.db"
SNAPSHOT_PATH = _REPO / "ops" / "pwa" / "serve" / "lp_agile_latest.json"
APR_LEADERBOARD_PATH = _REPO / "ops" / "pwa" / "serve" / "lp_apr_leaderboard.json"


def get_per_pool_floor_usd(chain: str) -> float:
    """Per-chain capital floor. Env-tunable per chain + global fallback."""
    try:
        return float(
            os.environ.get(f"LP_PER_POOL_FLOOR_USD_{chain.upper()}")
            or os.environ.get("LP_PER_POOL_FLOOR_USD", "500")
        )
    except ValueError:
        return 500.0


def _read_live_values_from_snapshot() -> dict:
    """Map nft_token_id → {value_usd, projected_apr_pct, ...} from chain snapshot."""
    try:
        d = json.loads(SNAPSHOT_PATH.read_text())
    except Exception:
        return {}
    out = {}
    for p in d.get("open_positions") or []:
        nft = p.get("nft_token_id")
        if nft is None:
            continue
        try:
            nft_int = int(nft)
        except (TypeError, ValueError):
            continue
        out[nft_int] = {
            "value_usd": float(p.get("value_usd") or 0.0),
            "fees_owed_usd": float(p.get("fees_owed_usd") or 0.0),
            "projected_apr_pct": float(
                p.get("projected_apr_pct")
                or p.get("apy_total_pct")
                or 0.0
            ),
            "pair": p.get("pair"),
            "protocol": p.get("protocol"),
            "staked": bool(p.get("staked")),
            "staked_in_gauge": p.get("staked_in_gauge"),
        }
    return out


def _read_leaderboard_apr() -> dict:
    """Map (chain, pair_normalized) → apy_pct from the APR scanner output.

    Pair normalization: strip whitespace, uppercase, sorted tokens
    (e.g., "WHYPE/USDC" → "USDC_WHYPE", matches "USDC-WHYPE").
    """
    try:
        d = json.loads(APR_LEADERBOARD_PATH.read_text())
    except Exception:
        return {}
    out = {}
    for chain, info in (d.get("chains") or {}).items():
        for p in info.get("top") or []:
            sym = (p.get("symbol") or "").upper()
            # Normalize: "WHYPE-USDC" → frozenset({"WHYPE","USDC"})
            tokens = frozenset(t for t in sym.replace("/", "-").split("-") if t)
            key = (chain.lower(), tokens)
            # Take MAX if multiple projects offer same pair
            if key not in out or p["apy_pct"] > out[key]:
                out[key] = p["apy_pct"]
    return out


def _lookup_apr_for_pair(chain: str, pair: str, leaderboard: dict) -> float:
    """Best-effort APR lookup for a pair on a chain."""
    sym = (pair or "").upper().replace("/", "-")
    tokens = frozenset(t for t in sym.split("-") if t)
    return leaderboard.get((chain.lower(), tokens), 0.0)


def _read_open_positions(chain: str, protocol: Optional[str] = None) -> list[dict]:
    """All open managed_positions on a chain (optionally filtered by protocol)."""
    try:
        conn = sqlite3.connect(str(MANAGED_DB))
        conn.row_factory = sqlite3.Row
        sql = (
            "SELECT * FROM managed_positions "
            "WHERE chain=? AND status='open' "
            "AND current_nft_token_id IS NOT NULL"
        )
        params = [chain]
        if protocol:
            sql += " AND protocol=?"
            params.append(protocol)
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("managed_positions read failed: %s", exc)
        return []


def pick_target_position(
    *, chain: str, protocol: Optional[str] = None,
    per_pool_floor_usd: Optional[float] = None,
) -> Optional[dict]:
    """Pick the next NFT to top up on a chain.

    Args:
      chain: 'base' or 'hyperevm'
      protocol: optional filter ('slipstream' or 'prjx')
      per_pool_floor_usd: override the per-chain floor (else env-default)

    Returns:
      Dict describing the chosen position, or None if no eligible positions.

    Algorithm:
      1. Load all open managed_positions on chain (optionally by protocol)
      2. Enrich each with live value_usd + projected APR from snapshot
      3. Partition into below-floor vs at-floor
      4. If any below-floor: pick LARGEST GAP (most under-funded first)
      5. Else: pick HIGHEST APR (deploy to compound yield)
      6. Tie-breaker: larger existing value
    """
    floor = per_pool_floor_usd or get_per_pool_floor_usd(chain)
    positions = _read_open_positions(chain, protocol)
    if not positions:
        return None

    live = _read_live_values_from_snapshot()
    leaderboard = _read_leaderboard_apr()

    # Enrich each with live data + best available APR
    enriched = []
    for p in positions:
        nft = int(p.get("current_nft_token_id") or 0)
        live_p = live.get(nft, {})
        current_value = live_p.get("value_usd") or float(
            p.get("lifetime_capital_in_usd") or 0
        )
        token0_sym = p.get("token0_symbol") or "?"
        token1_sym = p.get("token1_symbol") or "?"
        pair = f"{token0_sym}/{token1_sym}"

        # APR resolution (priority order):
        #   1. DeFiLlama leaderboard (freshest, market-wide)
        #   2. Chain snapshot (computed per-position)
        #   3. 0% (no data yet — picker falls back to tie-breaker)
        leaderboard_apr = _lookup_apr_for_pair(chain, pair, leaderboard)
        snapshot_apr = live_p.get("projected_apr_pct") or 0.0
        apr = max(leaderboard_apr, snapshot_apr)

        enriched.append({
            "id": p["id"],
            "nft_token_id": nft,
            "pair": pair,
            "protocol": p.get("protocol"),
            "chain": p.get("chain"),
            "current_value_usd": current_value,
            "projected_apr_pct": apr,
            "apr_source": (
                "leaderboard" if leaderboard_apr > 0
                else "snapshot" if snapshot_apr > 0
                else "none"
            ),
            "below_floor": current_value < floor,
            "fill_gap_usd": max(0.0, floor - current_value),
            "is_staked": live_p.get("staked", False),
            "gauge_address": live_p.get("staked_in_gauge"),
        })

    if not enriched:
        return None

    # HIGHEST APR WINS, ALWAYS (Yomi 2026-06-04: "It's all about ROI").
    # Tie-breaker: larger current_value_usd (concentrate over fragment).
    winner = max(
        enriched,
        key=lambda p: (p["projected_apr_pct"], p["current_value_usd"]),
    )
    winner["reason"] = (
        f"highest_apr: {winner['projected_apr_pct']:.2f}% "
        f"({winner['apr_source']}), value ${winner['current_value_usd']:.2f}"
        + (f" (still below ${floor:.0f} floor)" if winner["below_floor"] else "")
    )
    winner["per_pool_floor_usd"] = floor
    return winner


def get_target_summary(chain: str, protocol: Optional[str] = None) -> dict:
    """Diagnostic helper — return the chosen target + the ranked alternates."""
    floor = get_per_pool_floor_usd(chain)
    positions = _read_open_positions(chain, protocol)
    live = _read_live_values_from_snapshot()
    leaderboard = _read_leaderboard_apr()
    rows = []
    for p in positions:
        nft = int(p.get("current_nft_token_id") or 0)
        live_p = live.get(nft, {})
        current = live_p.get("value_usd") or float(
            p.get("lifetime_capital_in_usd") or 0
        )
        pair = f"{p.get('token0_symbol')}/{p.get('token1_symbol')}"
        leaderboard_apr = _lookup_apr_for_pair(chain, pair, leaderboard)
        snapshot_apr = live_p.get("projected_apr_pct") or 0.0
        apr = max(leaderboard_apr, snapshot_apr)
        rows.append({
            "id": p["id"],
            "nft_token_id": nft,
            "pair": pair,
            "current_value_usd": round(current, 2),
            "projected_apr_pct": round(apr, 2),
            "apr_source": (
                "leaderboard" if leaderboard_apr > 0
                else "snapshot" if snapshot_apr > 0
                else "none"
            ),
            "below_floor": current < floor,
            "fill_gap_usd": round(max(0.0, floor - current), 2),
        })
    target = pick_target_position(chain=chain, protocol=protocol)
    return {
        "chain": chain,
        "protocol": protocol,
        "per_pool_floor_usd": floor,
        "positions": rows,
        "chosen": target,
    }


if __name__ == "__main__":
    # CLI: python -m engine.strategies.lp_agile.idle_deploy_picker base
    import sys
    chain = sys.argv[1] if len(sys.argv) > 1 else "hyperevm"
    proto = sys.argv[2] if len(sys.argv) > 2 else None
    print(json.dumps(get_target_summary(chain, proto), indent=2, default=str))
