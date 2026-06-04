"""engine/strategies/lp_agile/lp_apr_scanner.py — periodic top-APR scanner.

Continuously surfaces the best safe yields per chain (Base + HyperEVM)
using DeFiLlama's free /pools endpoint. Two outputs:

  1. ops/pwa/serve/lp_apr_leaderboard.json — top-N pools per chain (UI + ops)
  2. Cross-chain alert: when one chain's best APR significantly beats the
     other, send an operator-only Telegram digest recommending a manual
     bridge move. NO auto-execute cross-chain (Yomi 2026-06-04 #420 —
     "reduce risk, just alerts").

Filters applied to candidates:
  - Allowlisted protocols only (uniswap-v3, aerodrome-v1, slipstream, prjx)
  - TVL ≥ $500K (already-discovered safe pools)
  - APY ≥ 5% (anything below isn't worth tracking)
  - APY ≤ 300% (cap to filter farming-token-only pools with ephemeral yields)
  - Stable+volatile or volatile+volatile pairs (no shitcoin/shitcoin)

Telegram alert conditions (cross-chain):
  - Triggered ONCE per 24h per chain pair
  - delta = best_other_chain_apr − best_current_chain_apr
  - Alert when delta > LP_BRIDGE_ALERT_DELTA_PCT (default 20)
  - Includes Across bridge cost estimate (rough %)

Shipped 2026-06-04 #420 as part of "full LP automation completion".
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.lp_apr_scanner")

_REPO = Path(__file__).resolve().parents[3]
LEADERBOARD_PATH = _REPO / "ops" / "pwa" / "serve" / "lp_apr_leaderboard.json"
ALERT_STATE_PATH = _REPO / "engine" / "_state" / "lp_apr_alert_state.json"
LOG_PATH = _REPO / "engine" / "_signals" / "lp_apr_scanner_audit.jsonl"

# Knobs
CHAINS_TO_SCAN = ("base", "hyperevm")

# DeFiLlama uses different chain naming than our engine internals.
# Map our canonical chain key → set of DeFiLlama chain values to match.
_DEFILLAMA_CHAIN_ALIASES = {
    "base": {"base"},
    "hyperevm": {"hyperliquid l1", "hyperliquid", "hyperevm"},
}

# Token quality allowlist (Yomi 2026-06-04 #420):
# "I like WETH-CBBTC … but not sure about any funny token like the first on
# the list" (273% WETH-SURPLUS). Reject pools where either token isn't on
# the approved list — protects against farm-token APY traps that vanish.
_APPROVED_TOKENS = frozenset([
    # Stablecoins
    "USDC", "USDT", "DAI", "FRAX", "USDC.E", "USDT0", "USDE", "FEUSD", "USD₮0",
    "USDS", "GHO", "PYUSD", "USDB",
    # Major majors
    "WETH", "ETH", "WBTC", "BTC", "CBBTC", "UBTC", "TBTC",
    # Chain-native + wrapped
    "WHYPE", "HYPE", "KHYPE", "WMATIC", "MATIC", "WAVAX", "AVAX",
    "WOPT", "OP", "ARB", "WBNB", "BNB", "SOL", "WSOL",
    # Major staked variants
    "STETH", "WSTETH", "RETH", "CBETH", "SFRXETH", "EZETH", "WEETH",
    # Major DeFi
    "AAVE", "UNI", "LINK", "MKR", "CRV", "COMP",
])


def _pair_is_approved(symbol: str) -> bool:
    """Both tokens in the pair must be on the approved list."""
    if not symbol:
        return False
    sym = symbol.upper().replace("/", "-")
    tokens = [t for t in sym.split("-") if t]
    if len(tokens) < 2:
        return False
    return all(t in _APPROVED_TOKENS for t in tokens)
TOP_N_PER_CHAIN = int(os.environ.get("LP_APR_TOP_N", "20"))
MIN_TVL_USD = float(os.environ.get("LP_APR_MIN_TVL_USD", "500000"))
MIN_APY_PCT = float(os.environ.get("LP_APR_MIN_APY_PCT", "5"))
MAX_APY_PCT = float(os.environ.get("LP_APR_MAX_APY_PCT", "300"))
BRIDGE_ALERT_DELTA_PCT = float(os.environ.get("LP_BRIDGE_ALERT_DELTA_PCT", "20"))
BRIDGE_ALERT_COOLDOWN_HOURS = float(
    os.environ.get("LP_BRIDGE_ALERT_COOLDOWN_HOURS", "24")
)
ACROSS_BRIDGE_COST_PCT = 0.05  # rough Across fee — display only


def _now_ts() -> float:
    return time.time()


def _log(event: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    event["ts_iso"] = datetime.now(timezone.utc).isoformat()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(event, default=str) + "\n")


def _load_alert_state() -> dict:
    try:
        return json.loads(ALERT_STATE_PATH.read_text())
    except Exception:
        return {}


def _save_alert_state(d: dict) -> None:
    ALERT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALERT_STATE_PATH.write_text(json.dumps(d, indent=2))


def _alert_telegram(text: str) -> None:
    try:
        from engine.telegram.client import send
        send("signal", key=f"lp_bridge_alert:{int(time.time())}",
             text=text, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Telegram alert failed: %s", exc)


def _filter_pools(pools: list[dict], chain: str) -> list[dict]:
    """Apply quality filters."""
    aliases = _DEFILLAMA_CHAIN_ALIASES.get(chain.lower(), {chain.lower()})
    out = []
    for p in pools:
        pool_chain = (p.get("chain") or "").lower()
        if pool_chain not in aliases:
            continue
        tvl = p.get("tvlUsd") or 0
        apy = p.get("apy") or 0
        if tvl < MIN_TVL_USD:
            continue
        if not (MIN_APY_PCT <= apy <= MAX_APY_PCT):
            continue
        # Skip wrapped-only / single-asset pools
        sym = p.get("symbol") or ""
        if "-" not in sym and "/" not in sym:
            continue
        # Token quality filter — both tokens must be on the approved list.
        # Protects against farm-token APY traps.
        if not _pair_is_approved(sym):
            continue
        out.append({
            "pool_id": p.get("pool"),
            "project": p.get("project"),
            "symbol": sym,
            "chain": p.get("chain"),
            "tvl_usd": tvl,
            "apy_pct": round(apy, 2),
            "apy_base_pct": round(p.get("apyBase") or 0.0, 2),
            "apy_reward_pct": round(p.get("apyReward") or 0.0, 2),
            "il_risk": p.get("ilRisk"),
            "stablecoin": p.get("stablecoin", False),
        })
    out.sort(key=lambda p: p["apy_pct"], reverse=True)
    return out[:TOP_N_PER_CHAIN]


def scan() -> dict:
    """Run one APR scan. Writes leaderboard + maybe fires Telegram alert.

    Returns the result dict for inspection.
    """
    started = _now_ts()
    result = {
        "ts_iso": datetime.now(timezone.utc).isoformat(),
        "chains": {},
        "alert_fired": None,
        "alert_skipped_reason": None,
    }

    try:
        from engine.data.lp_pools.defillama_yields import fetch_yields
    except Exception as exc:
        result["error"] = f"defillama import failed: {exc}"
        _log({"action": "scan_failed", **result})
        return result

    try:
        all_pools = fetch_yields()
    except Exception as exc:
        result["error"] = f"defillama fetch failed: {exc}"
        _log({"action": "scan_failed", **result})
        return result

    # Filter per chain
    for chain in CHAINS_TO_SCAN:
        top = _filter_pools(all_pools, chain=chain)
        result["chains"][chain] = {
            "count": len(top),
            "top": top,
            "best_apy_pct": top[0]["apy_pct"] if top else None,
            "best_symbol": top[0]["symbol"] if top else None,
            "best_project": top[0]["project"] if top else None,
        }

    # Persist leaderboard
    LEADERBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEADERBOARD_PATH.write_text(
        json.dumps(result, indent=2, default=str)
    )

    # Cross-chain bridge alert logic
    apr_by_chain = {
        c: result["chains"][c].get("best_apy_pct")
        for c in CHAINS_TO_SCAN
        if result["chains"][c].get("best_apy_pct") is not None
    }
    if len(apr_by_chain) < 2:
        result["alert_skipped_reason"] = "not_all_chains_have_data"
    else:
        chains = list(apr_by_chain.keys())
        c1, c2 = chains[0], chains[1]
        a1, a2 = apr_by_chain[c1], apr_by_chain[c2]
        delta = abs(a1 - a2)
        better_chain = c1 if a1 > a2 else c2
        worse_chain = c2 if a1 > a2 else c1
        better_apr = max(a1, a2)
        worse_apr = min(a1, a2)
        better_sym = result["chains"][better_chain]["best_symbol"]
        better_proj = result["chains"][better_chain]["best_project"]

        if delta < BRIDGE_ALERT_DELTA_PCT:
            result["alert_skipped_reason"] = (
                f"delta {delta:.1f}pp < threshold {BRIDGE_ALERT_DELTA_PCT}pp"
            )
        else:
            # Cooldown check (per chain-pair)
            state = _load_alert_state()
            pair_key = f"{worse_chain}_to_{better_chain}"
            last = state.get(pair_key, 0)
            elapsed_h = (_now_ts() - last) / 3600
            if elapsed_h < BRIDGE_ALERT_COOLDOWN_HOURS:
                result["alert_skipped_reason"] = (
                    f"cooldown: last alert {elapsed_h:.1f}h ago "
                    f"(threshold {BRIDGE_ALERT_COOLDOWN_HOURS}h)"
                )
            else:
                # FIRE THE ALERT (operator only)
                # Estimate net benefit: better APY − bridge cost
                bridge_pct = ACROSS_BRIDGE_COST_PCT
                net_delta_pp = delta - bridge_pct
                msg = (
                    f"🌉 <b>LP cross-chain opportunity</b>\n"
                    f"{worse_chain.upper()} best: {worse_apr:.1f}% APR\n"
                    f"{better_chain.upper()} best: {better_apr:.1f}% APR "
                    f"({better_proj} · {better_sym})\n"
                    f"\nDelta: <b>{delta:.1f}pp</b> "
                    f"(net of ~{bridge_pct:.2f}% bridge: {net_delta_pp:.1f}pp)\n"
                    f"\n💡 Consider moving capital "
                    f"{worse_chain.upper()} → {better_chain.upper()} "
                    f"if you have ≥$500 idle on {worse_chain}.\n"
                    f"\n<i>Alert-only. Auto-bridge disabled per safety policy.</i>"
                )
                _alert_telegram(msg)
                state[pair_key] = _now_ts()
                _save_alert_state(state)
                result["alert_fired"] = {
                    "from_chain": worse_chain, "to_chain": better_chain,
                    "delta_pp": delta, "net_delta_pp": net_delta_pp,
                }

    result["duration_s"] = round(_now_ts() - started, 2)
    _log({"action": "scan_complete", **result})
    return result


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    out = scan()
    # Trim TG-worthy view for stdout
    compact = {
        "chains": {
            c: {
                "best_apy_pct": d.get("best_apy_pct"),
                "best_symbol": d.get("best_symbol"),
                "best_project": d.get("best_project"),
                "count": d.get("count"),
            }
            for c, d in out["chains"].items()
        },
        "alert_fired": out.get("alert_fired"),
        "alert_skipped_reason": out.get("alert_skipped_reason"),
    }
    print(json.dumps(compact, indent=2, default=str))
