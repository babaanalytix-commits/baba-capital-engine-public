"""engine/strategies/lp_agile/nav.py — Phase 3 of LP pivot.

Computes the pool's total NAV from managed_positions + their current on-chain
state (read from lp_positions_latest.json — the snapshot the LP scanner
already maintains).

NAV per managed position =
    current_amount0_usd
  + current_amount1_usd
  + uncollected_fees_usd
  − lifetime_gas_usd          (gas is a cost against the position's lifetime
                               value, not a per-tick deduction)

Pool NAV = Σ NAV(position) across all open managed positions.

Then capital_pool.compute_shares(nav) distributes share + interest to
contributors using the existing logic.

Honest scope: today's lp_positions_latest.json may not include every field
(uncollected fees + amount0/1 prices) for every protocol — we read what's
available, log gaps, and default missing fields to 0. The trigger-engine
+ harvester will populate them progressively.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.nav")

_REPO_ROOT = Path(__file__).resolve().parents[3]
# 2026-05-30 fix: actual snapshot path is the PWA serve file, not the
# previously-assumed engine/_reports/lp_positions_latest.json.
LP_LATEST = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_agile_latest.json"


def _load_latest_positions() -> list[dict]:
    if not LP_LATEST.exists():
        return []
    try:
        d = json.loads(LP_LATEST.read_text())
        return d.get("open_positions") or d.get("positions") or []
    except Exception as exc:
        logger.warning("[nav] failed to load %s: %s", LP_LATEST, exc)
        return []


def _position_nav_usd(live_position: dict, managed_row: dict) -> dict:
    """Compute NAV for one managed position. Reads the actual schema from
    ops/pwa/serve/lp_agile_latest.json: {value_usd, fees_owed_usd, ...}."""
    # Single combined USD value as the LP scanner provides it
    value_usd = float(live_position.get("value_usd") or 0)
    # Some scanners may also split into amount0/amount1 — sum them if present
    amount0_usd = float(live_position.get("amount0_usd") or 0)
    amount1_usd = float(live_position.get("amount1_usd") or 0)
    if (amount0_usd + amount1_usd) > value_usd:
        value_usd = amount0_usd + amount1_usd
    uncollected_fees_usd = float(
        live_position.get("fees_owed_usd")
        or live_position.get("uncollected_fees_usd")
        or 0
    )
    lifetime_gas_usd = float(managed_row.get("lifetime_gas_usd") or 0)
    pending_rewards_usd = float(live_position.get("pending_aero_usd") or 0)
    nav = (value_usd
           + uncollected_fees_usd
           + pending_rewards_usd
           - lifetime_gas_usd)
    return {
        "managed_position_id": managed_row.get("id"),
        "chain": managed_row.get("chain"),
        "protocol": managed_row.get("protocol"),
        "pair": (f"{managed_row.get('token0_symbol') or '?'}/"
                 f"{managed_row.get('token1_symbol') or '?'}"),
        "fee_tier": managed_row.get("fee_tier"),
        "current_nft_token_id": managed_row.get("current_nft_token_id"),
        "live_value_usd": round(value_usd, 2),
        "uncollected_fees_usd": round(uncollected_fees_usd, 4),
        "pending_rewards_usd": round(pending_rewards_usd, 4),
        "lifetime_gas_usd": round(lifetime_gas_usd, 4),
        "lifetime_fees_collected_usd": round(
            float(managed_row.get("lifetime_fees_collected_usd") or 0), 4
        ),
        "nav_usd": round(nav, 2),
    }


def _match_live_to_managed(live_positions: list[dict],
                            managed_rows: list[dict]) -> dict:
    """Build (managed_position_id → live position dict) lookup. The PWA
    snapshot doesn't carry chain/fee_tier on each row, so we match in two
    passes: first by current_nft_token_id (most specific), then by pool
    address (covers NFT rotation between snapshots)."""
    by_nft: dict[int, dict] = {}
    by_pool: dict[str, dict] = {}
    for lp in live_positions:
        nft = lp.get("nft_token_id") or lp.get("token_id")
        if nft is not None:
            try:
                by_nft[int(nft)] = lp
            except Exception:
                pass
        pool = (lp.get("pool_address") or "").lower()
        if pool:
            by_pool[pool] = lp
    out = {}
    for mp in managed_rows:
        nft = mp.get("current_nft_token_id")
        live = by_nft.get(int(nft)) if nft is not None else None
        if not live:
            live = by_pool.get((mp.get("pool_address") or "").lower())
        if live is not None:
            out[mp["id"]] = (live, mp)
        else:
            logger.info(
                "[nav] managed_position id=%s (%s/%s) has no live state — "
                "NAV = -lifetime_gas only",
                mp.get("id"), mp.get("chain"), mp.get("pool_address"),
            )
            out[mp["id"]] = ({}, mp)
    return out


def compute_pool_nav(*, include_breakdown: bool = False) -> dict:
    """Sum NAV across all open managed positions. Returns:
        {"total_nav_usd": float, "n_positions": int,
         "positions": [per-position breakdown if include_breakdown=True],
         "warnings": [...]}.
    """
    from engine.strategies.lp_agile import managed_position as MP
    managed = MP.list_managed_positions(status="open")
    live = _load_latest_positions()
    matched = _match_live_to_managed(live, managed)
    breakdown = []
    total = 0.0
    warnings = []
    for mp_id, (live_p, mp) in matched.items():
        if not live_p:
            warnings.append(
                f"managed_position id={mp_id} has no live state — NAV"
                " contribution = -lifetime_gas only"
            )
        row = _position_nav_usd(live_p, mp)
        total += row["nav_usd"]
        if include_breakdown:
            breakdown.append(row)
    out = {
        "total_nav_usd": round(total, 2),
        "n_positions": len(matched),
        "warnings": warnings,
    }
    if include_breakdown:
        out["positions"] = breakdown
    return out


def compute_shares_from_managed(*, include_breakdown: bool = False) -> dict:
    """One-shot: compute pool NAV from managed_positions, then run the
    capital_pool share/interest distribution against it."""
    from engine.strategies.lp_agile import capital_pool as CP
    nav = compute_pool_nav(include_breakdown=include_breakdown)
    shares = CP.compute_shares(nav["total_nav_usd"])
    out = {
        "total_nav_usd": nav["total_nav_usd"],
        "n_positions": nav["n_positions"],
        "warnings": nav["warnings"],
        "contributors": shares.get("contributors") or [],
        "yomi_cut_total_usd": shares.get("yomi_cut_total_usd"),
        "total_invested_net": shares.get("total_invested_net"),
        "growth_total_usd": shares.get("growth_total_usd"),
    }
    if include_breakdown:
        out["positions"] = nav.get("positions") or []
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(prog="nav")
    ap.add_argument("--breakdown", action="store_true")
    ap.add_argument("--shares", action="store_true",
                    help="Also compute contributor shares")
    args = ap.parse_args()
    if args.shares:
        out = compute_shares_from_managed(include_breakdown=args.breakdown)
    else:
        out = compute_pool_nav(include_breakdown=args.breakdown)
    print(json.dumps(out, indent=2, default=str))
