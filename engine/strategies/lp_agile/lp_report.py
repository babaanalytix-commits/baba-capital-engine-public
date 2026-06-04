"""engine/strategies/lp_agile/lp_report.py — consolidated LP portfolio report.

LP-REVAMP P2 (2026-06-01). One place to see the whole LP book honestly:
per-position fees / IL / gas / NET + in/out-of-range, and a portfolio rollup
with net APR, exposure by chain/base-asset/protocol, circuit-breaker state, and
the currently-harvestable income.

Sources (all READ-ONLY — this module signs nothing):
  - ops/pwa/serve/lp_agile_latest.json  → LIVE truth per position: in_range,
    value_usd, fees_owed_usd, pool_price_now, range_low/high.
  - managed_position.py (lp_managed_positions.db) → LIFETIME metrics: gas,
    fees collected, realized IL, realized APR, opened_at.
  - cost_ledger.realized_il_from_model → live UNREALISED IL estimate (V3 math,
    LP-2), so NET reflects divergence, not just fees.
  - lp_circuit_breaker.state() → armed/disarmed.

Output: a dict (→ JSON for the PWA) + a pretty CLI summary.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.lp_report")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SNAPSHOT = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_agile_latest.json"
_SERVE_OUT = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_report.json"  # PWA/console consumes this

# protocol → chain (for exposure grouping + display)
_PROTOCOL_CHAIN = {
    "prjx": "hyperevm",
    "slipstream": "base",
    "aerodrome": "base",
    "uniswap_v3": "ethereum",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_snapshot() -> dict:
    try:
        return json.loads(_SNAPSHOT.read_text()) if _SNAPSHOT.exists() else {}
    except Exception as exc:
        logger.warning("[lp_report] snapshot read failed: %s", exc)
        return {}


def _managed_by_pool() -> dict:
    """pool_address(lower) → managed_position row (lifetime metrics)."""
    try:
        from engine.strategies.lp_agile.managed_position import list_managed_positions
        out = {}
        for mp in list_managed_positions(status="open"):
            addr = (mp.get("pool_address") or "").lower()
            if addr:
                out[addr] = mp
        return out
    except Exception as exc:
        logger.info("[lp_report] managed positions unavailable: %s", exc)
        return {}


def _live_il_usd(value_usd: float, price_now: float, low: float, high: float) -> Optional[float]:
    """Best-effort UNREALISED IL for an open position: entry ≈ geometric centre
    of the range (compute_range builds ranges around entry), valued at price_now.
    Uses the same V3 math as the backtest/close accounting (LP-2)."""
    try:
        if not (price_now and low and high and value_usd) or low <= 0 or high <= 0:
            return None
        from engine.strategies.lp_agile.cost_ledger import realized_il_from_model
        entry = math.sqrt(low * high)
        r = realized_il_from_model(
            entry_price=Decimal(str(entry)), exit_price=Decimal(str(price_now)),
            deposited_usd=Decimal(str(value_usd)),
            range_low=Decimal(str(low)), range_high=Decimal(str(high)),
        )
        return float(r["il_usd"])
    except Exception as exc:
        logger.debug("[lp_report] live IL calc skipped: %s", exc)
        return None


def _age_days(opened_iso: Optional[str]) -> Optional[float]:
    if not opened_iso:
        return None
    try:
        opened = datetime.fromisoformat(opened_iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - opened).total_seconds() / 86400.0
    except Exception:
        return None


def _venue_yields() -> list:
    """DeFiLlama pool rows (cached) for venue-grade APR + discovery. Never raises."""
    try:
        from engine.data.lp_pools import defillama_yields as dl
        return dl.fetch_yields()
    except Exception as exc:
        logger.info("[lp_report] DeFiLlama yields unavailable: %s", exc)
        return []


def build_lp_report() -> dict:
    snap = _load_snapshot()
    positions = snap.get("open_positions") or []
    managed = _managed_by_pool()
    venue_pools = _venue_yields()

    rows = []
    tot_value = tot_fees_owed = tot_lifetime_fees = tot_gas = tot_il = 0.0
    by_chain: dict[str, float] = {}
    by_base: dict[str, float] = {}
    by_protocol: dict[str, float] = {}
    out_of_range = []

    for p in positions:
        proto = (p.get("protocol") or "").lower()
        chain = _PROTOCOL_CHAIN.get(proto, "unknown")
        pair = p.get("pair") or "?"
        base = pair.split("/")[0].strip().upper() if "/" in pair else pair.upper()
        value = float(p.get("value_usd") or 0)
        fees_owed = float(p.get("fees_owed_usd") or 0)
        price = float(p.get("pool_price_now") or 0)
        low = float(p.get("range_low") or 0)
        high = float(p.get("range_high") or 0)
        in_range = bool(p.get("in_range"))

        mp = managed.get((p.get("pool_address") or "").lower(), {})
        lifetime_fees = float(mp.get("lifetime_fees_collected_usd") or 0)
        gas = float(mp.get("lifetime_gas_usd") or 0)
        realized_apr = mp.get("realized_apr_pct")
        age = _age_days(mp.get("opened_at_iso"))

        il = _live_il_usd(value, price, low, high)          # unrealised, may be None
        total_fees = fees_owed + lifetime_fees
        net = total_fees - gas - (il or 0.0)                # IL-aware net

        # Venue-grade APR (DeFiLlama apyBase+apyReward) — matches the prjx/
        # Aerodrome UI, unlike the DexScreener-volume estimate (P2b finding).
        venue = None
        if venue_pools:
            try:
                from engine.data.lp_pools import defillama_yields as _dl
                venue = _dl.lookup_apr(chain, pair, project=proto, pools=venue_pools)
            except Exception:
                venue = None

        if not in_range:
            out_of_range.append(pair)

        tot_value += value
        tot_fees_owed += fees_owed
        tot_lifetime_fees += lifetime_fees
        tot_gas += gas
        tot_il += (il or 0.0)
        by_chain[chain] = by_chain.get(chain, 0.0) + value
        by_base[base] = by_base.get(base, 0.0) + value
        by_protocol[proto] = by_protocol.get(proto, 0.0) + value

        rows.append({
            "pair": pair, "protocol": proto, "chain": chain,
            "nft_token_id": p.get("nft_token_id"),
            "in_range": in_range,
            "value_usd": round(value, 2),
            "price_now": price, "range_low": low, "range_high": high,
            "fees_uncollected_usd": round(fees_owed, 4),
            "fees_collected_lifetime_usd": round(lifetime_fees, 4),
            "gas_lifetime_usd": round(gas, 4),
            "il_unrealised_usd": round(il, 4) if il is not None else None,
            "net_pnl_usd": round(net, 4),
            "realized_apr_pct": round(realized_apr, 2) if isinstance(realized_apr, (int, float)) else None,
            # venue-grade APR (the number that matches the prjx/Aerodrome UI)
            "venue_apy_pct": venue.get("apy") if venue else None,
            "venue_apy_base_pct": venue.get("apy_base") if venue else None,
            "venue_apy_reward_pct": venue.get("apy_reward") if venue else None,
            "age_days": round(age, 1) if age is not None else None,
        })

    net_portfolio = (tot_fees_owed + tot_lifetime_fees) - tot_gas - tot_il
    pct = lambda v: round(v / tot_value * 100, 1) if tot_value > 0 else 0.0

    try:
        from engine.core.lp_circuit_breaker import state as _cb_state
        cb = _cb_state()
    except Exception:
        cb = {"active": None}

    return {
        "generated_at_iso": _now(),
        "snapshot_at_iso": snap.get("generated_at_iso"),
        "n_positions": len(rows),
        "n_out_of_range": len(out_of_range),
        "out_of_range_pairs": out_of_range,
        "positions": sorted(rows, key=lambda r: r["value_usd"], reverse=True),
        "portfolio": {
            "total_value_usd": round(tot_value, 2),
            "uncollected_fees_usd": round(tot_fees_owed, 2),       # harvestable now
            "lifetime_fees_collected_usd": round(tot_lifetime_fees, 2),
            "lifetime_gas_usd": round(tot_gas, 2),
            "unrealised_il_usd": round(tot_il, 2),
            "net_pnl_usd": round(net_portfolio, 2),                 # IL-aware
            "exposure_by_chain_pct": {k: pct(v) for k, v in sorted(by_chain.items())},
            "exposure_by_base_asset_pct": {k: pct(v) for k, v in sorted(by_base.items())},
            "exposure_by_protocol_pct": {k: pct(v) for k, v in sorted(by_protocol.items())},
            "circuit_breaker": cb,
        },
        "income": {
            "harvestable_now_usd": round(tot_fees_owed, 2),
            "note": "uncollected fees across positions — claimable income",
        },
        "discovery": _discovery_board(venue_pools),
    }


def _discovery_board(venue_pools: list) -> dict:
    """Top SAFE yields across all chains (info), + the best executable-now
    (Base/HyperEVM) candidates — the 'great yields elsewhere' board."""
    if not venue_pools:
        return {"available": False, "note": "DeFiLlama feed unavailable"}
    try:
        from engine.data.lp_pools import defillama_yields as dl
        all_top = dl.discover(pools=venue_pools, top_n=15)
        executable = [r for r in all_top if r.get("executable_now")][:8]
        return {"available": True, "top_all_chains": all_top, "executable_now": executable}
    except Exception as exc:
        logger.info("[lp_report] discovery board failed: %s", exc)
        return {"available": False, "note": str(exc)[:120]}


def print_pretty(rep: Optional[dict] = None) -> None:
    rep = rep or build_lp_report()
    pf = rep["portfolio"]
    print(f"\n{'='*74}")
    print(f"  BABA LP PORTFOLIO — {rep['generated_at_iso'][:19]}  "
          f"(snapshot {str(rep.get('snapshot_at_iso'))[:19]})")
    print(f"{'='*74}")
    print(f"  NAV ${pf['total_value_usd']:.2f}  ·  NET (IL-aware) ${pf['net_pnl_usd']:+.2f}  ·  "
          f"harvestable ${pf['income' if False else 'uncollected_fees_usd']:.2f}")
    cb = pf["circuit_breaker"]
    cb_str = "🚨 ARMED" if cb.get("active") else ("ok" if cb.get("active") is False else "?")
    print(f"  fees(life) ${pf['lifetime_fees_collected_usd']:.2f}  gas ${pf['lifetime_gas_usd']:.2f}  "
          f"unrealised IL ${pf['unrealised_il_usd']:.2f}  ·  circuit breaker: {cb_str}")
    print(f"  exposure — chain {pf['exposure_by_chain_pct']}  base {pf['exposure_by_base_asset_pct']}")
    if rep["n_out_of_range"]:
        print(f"  ⚠️  OUT OF RANGE ({rep['n_out_of_range']}): {', '.join(rep['out_of_range_pairs'])}")
    print(f"\n  {'PAIR':<14}{'CHAIN':<9}{'RANGE':<7}{'VALUE':>10}{'FEES':>9}{'IL':>9}{'NET':>9}{'APR':>8}")
    for r in rep["positions"]:
        rng = "in" if r["in_range"] else "OUT"
        il = f"{r['il_unrealised_usd']:.2f}" if r["il_unrealised_usd"] is not None else "—"
        apr = f"{r['realized_apr_pct']:.0f}%" if r["realized_apr_pct"] is not None else "—"
        fees = r["fees_uncollected_usd"] + r["fees_collected_lifetime_usd"]
        print(f"  {r['pair']:<14}{r['chain']:<9}{rng:<7}{r['value_usd']:>10.2f}"
              f"{fees:>9.2f}{il:>9}{r['net_pnl_usd']:>9.2f}{apr:>8}")
    print()


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Consolidated LP portfolio report")
    p.add_argument("--json", action="store_true", help="emit JSON instead of pretty")
    p.add_argument("--no-write", action="store_true", help="don't publish lp_report.json")
    args = p.parse_args(argv)
    rep = build_lp_report()
    if not args.no_write:
        try:
            _SERVE_OUT.parent.mkdir(parents=True, exist_ok=True)
            tmp = _SERVE_OUT.with_suffix(".tmp")
            tmp.write_text(json.dumps(rep, indent=2, default=str))
            tmp.replace(_SERVE_OUT)
        except Exception as exc:
            logger.warning("[lp_report] publish failed: %s", exc)
    print(json.dumps(rep, indent=2, default=str) if args.json else "", end="")
    if not args.json:
        print_pretty(rep)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
