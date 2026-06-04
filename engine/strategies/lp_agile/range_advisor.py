"""engine/strategies/lp_agile/range_advisor.py — LP range suggestion + out-of-range loop.

2026-05-27 (#39 middle-ground): the human-in-the-loop LP cockpit Yomi asked for.

THE IDEA (his words): "I get a suggested pool, with the minimum and maximum
range — from token performance and expected move, like sizing a trade, only
the horizon is the next month. The script checks regularly and alerts which
pool is out of range and what to do next (which pool, what min/max range), and
the loop continues. Over time, once proven, we automate. Plus a process to
identify new pools and add them."

WHAT THIS DOES (SUGGEST-ONLY — never executes; you act on the alert):
  1. expected_move_pct(asset, horizon_days)  — realized vol from HL daily
     candles → expected ±move over the horizon (default 30d).
  2. suggest_range(pool)                      — concentrated-LP min/max price
     band sized to that expected move. conservative/balanced/aggressive.
  3. check_positions()                        — for each open LP position, is
     price still inside its band? how close to an edge?
  4. run()                                    — emits a suggestion feed
     (lp_range_suggestions.json) + ONE Telegram alert ONLY when action is
     needed (out-of-range, or idle capital to deploy). Silent when all healthy.
  5. discover_pools()                         — pulls fresh scanner candidates
     so new pools enter the suggestion set automatically.

RANGE MATH: a wider band stays in-range longer (fewer rebalances, lower fee
concentration); narrower earns more fees but goes out-of-range sooner. We size
the band to ±k·σ_month where σ_month = σ_daily·√horizon:
  conservative k=1.5  · balanced k=1.0  · aggressive k=0.66

Zero AI. Pure stdlib + HL public candles + the existing LP scanner output.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.range_advisor")

_REPO = Path(__file__).resolve().parents[3]
HL_INFO_URL = "https://api.hyperliquid.xyz/info"
SCANNER_FEED = _REPO / "ops" / "pwa" / "serve" / "lp_agile_latest.json"
SUGGEST_FEEDS = [
    _REPO / "ops" / "pwa" / "serve" / "lp_range_suggestions.json",
    _REPO / "engine" / "_reports" / "lp_range_suggestions.json",
]
POSITIONS_FILE = _REPO / "engine" / "_reports" / "lp_positions_latest.json"

HORIZON_DAYS = int(os.environ.get("LP_RANGE_HORIZON_DAYS", "30"))
RANGE_MODE = os.environ.get("LP_RANGE_MODE", "balanced")   # conservative|balanced|aggressive
_K = {"conservative": 1.5, "balanced": 1.0, "aggressive": 0.66}
EDGE_ALERT_PCT = float(os.environ.get("LP_RANGE_EDGE_ALERT_PCT", "15"))  # warn within 15% of a band edge

# Map an LP pool's volatility-driving asset to the HL perp symbol we read vol from.
# (cbBTC tracks BTC; HYPE/WETH/SOL map to themselves.)
_VOL_PROXY = {
    "CBBTC": "BTC", "WBTC": "BTC", "BTC": "BTC",
    "WETH": "ETH", "ETH": "ETH",
    "HYPE": "HYPE", "SOL": "SOL", "USDC": None, "USDT": None,
}


@dataclass
class RangeSuggestion:
    pool: str
    base_asset: str
    quote_asset: str
    current_price: Optional[float]
    expected_move_pct: Optional[float]
    range_min: Optional[float]
    range_max: Optional[float]
    mode: str
    fee_apr_pct: Optional[float]
    tvl_usd: Optional[float]
    rationale: str


def _hl_daily_candles(asset: str, days: int = 120) -> list:
    try:
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - days * 86400 * 1000
        body = json.dumps({"type": "candleSnapshot", "req": {
            "coin": asset, "interval": "1d", "startTime": start_ms, "endTime": now_ms}}).encode()
        req = urllib.request.Request(HL_INFO_URL, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as r:
            d = json.loads(r.read().decode())
            return d if isinstance(d, list) else []
    except Exception as exc:
        logger.warning("[range_advisor] candles fetch failed for %s: %s", asset, exc)
        return []


def _vol_and_price(asset: str, horizon_days: int = HORIZON_DAYS) -> tuple[Optional[float], Optional[float]]:
    """Return (expected_move_fraction, latest_close_usd) from HL daily candles.
    σ_month = σ_daily·√horizon. latest_close is used as the pool's current price
    when the scanner feed doesn't carry one (USDC-quoted pools ≈ base USD price)."""
    proxy = _VOL_PROXY.get(asset.upper(), asset.upper())
    if proxy is None:
        return 0.01, 1.0  # stablecoin leg — tiny band, ~$1
    candles = _hl_daily_candles(proxy, days=max(90, horizon_days * 3))
    closes = []
    for c in candles:
        try:
            v = float(c.get("c") or 0)
            if v > 0:
                closes.append(v)
        except Exception:
            continue
    if len(closes) < 20:
        return None, (closes[-1] if closes else None)
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    sigma_daily = math.sqrt(var)
    return sigma_daily * math.sqrt(horizon_days), closes[-1]


def expected_move_pct(asset: str, horizon_days: int = HORIZON_DAYS) -> Optional[float]:
    """±expected move (fraction) over the horizon. Wrapper over _vol_and_price."""
    return _vol_and_price(asset, horizon_days)[0]


def suggest_range(pool: dict, *, mode: str = RANGE_MODE) -> RangeSuggestion:
    """Build a min/max LP band for a scanner pool dict (from lp_agile_latest)."""
    pair = pool.get("pair") or pool.get("pool_pair") or "?"
    base, _, quote = pair.partition("/")
    base = base.strip().upper(); quote = quote.strip().upper()
    price = pool.get("base_price") or pool.get("current_price") or pool.get("mark_price")
    try:
        price = float(price) if price is not None else None
    except Exception:
        price = None
    k = _K.get(mode, 1.0)
    move, candle_price = _vol_and_price(base)
    # Scanner feed often omits price → use the candle close (USDC-quoted pools
    # ≈ base USD price; good enough to seed a range the operator confirms).
    if price is None:
        price = candle_price
    rmin = rmax = None
    if price and move is not None:
        band = move * k
        rmin = round(price * (1 - band), 8)
        rmax = round(price * (1 + band), 8)
    rationale = (
        f"{base} ±{move*100:.1f}% expected over {HORIZON_DAYS}d (σ-scaled), "
        f"{mode} band ×{k} → keep capital in-range while earning fees."
        if move is not None else
        f"vol unavailable for {base} — widen manually before deploying."
    )
    return RangeSuggestion(
        pool=pair, base_asset=base, quote_asset=quote, current_price=price,
        expected_move_pct=(round(move * 100, 2) if move is not None else None),
        range_min=rmin, range_max=rmax, mode=mode,
        fee_apr_pct=pool.get("fee_apr_pct"), tvl_usd=pool.get("tvl_usd"),
        rationale=rationale,
    )


def _load_scanner_pools() -> list:
    try:
        d = json.loads(SCANNER_FEED.read_text())
        return d.get("ranked_pools") or []
    except Exception:
        return []


def _load_open_positions() -> list:
    try:
        d = json.loads(POSITIONS_FILE.read_text())
        return d.get("positions") or d if isinstance(d, list) else d.get("positions", [])
    except Exception:
        return []


def check_positions() -> list[dict]:
    """For each open LP position, classify in-range / near-edge / OUT-of-range."""
    out = []
    for p in _load_open_positions():
        try:
            cur = float(p.get("current_price") or p.get("price") or 0)
            lo = float(p.get("range_min") or p.get("tick_low") or 0)
            hi = float(p.get("range_max") or p.get("tick_high") or 0)
        except Exception:
            continue
        if not (cur and lo and hi):
            continue
        if cur < lo or cur > hi:
            status = "OUT_OF_RANGE"
        else:
            span = hi - lo
            near = min(cur - lo, hi - cur) / span * 100 if span else 0
            status = "NEAR_EDGE" if near < EDGE_ALERT_PCT else "IN_RANGE"
        out.append({"pool": p.get("pair") or p.get("pool"), "current_price": cur,
                    "range_min": lo, "range_max": hi, "status": status,
                    "value_usd": p.get("value_usd") or p.get("size_usd")})
    return out


def discover_pools(top_n: int = 6, *, mode: str = RANGE_MODE) -> list[RangeSuggestion]:
    """Pull fresh scanner candidates → range suggestions. New pools that enter
    the scanner ranking automatically appear here."""
    pools = _load_scanner_pools()[:top_n]
    return [suggest_range(p, mode=mode) for p in pools]


def run(*, telegram: bool = True) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    positions = check_positions()
    suggestions = discover_pools()
    out_of_range = [p for p in positions if p["status"] == "OUT_OF_RANGE"]
    near_edge = [p for p in positions if p["status"] == "NEAR_EDGE"]

    feed = {
        "generated_at_iso": now,
        "horizon_days": HORIZON_DAYS,
        "mode": RANGE_MODE,
        "positions": positions,
        "out_of_range": out_of_range,
        "near_edge": near_edge,
        "suggestions": [asdict(s) for s in suggestions],
    }
    for dest in SUGGEST_FEEDS:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(feed, indent=2, default=str))
        except Exception as exc:
            logger.warning("[range_advisor] feed write failed %s: %s", dest, exc)

    # Alert ONLY when action is needed (noise mandate): out-of-range, near-edge,
    # or idle capital with no positions. Silent when all in-range.
    actionable = out_of_range or near_edge or (not positions and suggestions)
    if telegram and actionable:
        lines = ["💧 <b>LP Range Advisor</b>"]
        for p in out_of_range:
            lines.append(f"🔴 <b>{p['pool']}</b> OUT OF RANGE (price ${p['current_price']}) — earning $0. Rebalance.")
        for p in near_edge:
            lines.append(f"🟡 {p['pool']} near band edge — watch / consider re-centering.")
        top = suggestions[0] if suggestions else None
        if top and top.range_min:
            lines.append(
                f"\n📋 <b>Suggested deploy:</b> {top.pool}\n"
                f"  Range: <b>${top.range_min} – ${top.range_max}</b> ({top.mode}, ±{top.expected_move_pct}% / {HORIZON_DAYS}d)\n"
                f"  Fee APR (headline): {top.fee_apr_pct}\n"
                f"  <i>{top.rationale}</i>"
            )
        try:
            from engine.telegram.client import send
            send("trade", key=f"lp_range:{now[:13]}", text="\n".join(lines), parse_mode="HTML")
        except Exception as exc:
            logger.warning("[range_advisor] telegram failed: %s", exc)

    logger.info("[range_advisor] %d positions (%d out-of-range, %d near-edge), %d suggestions",
                len(positions), len(out_of_range), len(near_edge), len(suggestions))
    return feed


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    f = run(telegram=False)
    print(f"\nHorizon {f['horizon_days']}d · mode {f['mode']}")
    print(f"Positions: {len(f['positions'])}  out-of-range: {len(f['out_of_range'])}\n")
    print("Top pool suggestions (deploy candidates):")
    for s in f["suggestions"]:
        if s["range_min"]:
            print(f"  {s['pool']:<16} ${s['current_price']:<10} range ${s['range_min']} – ${s['range_max']}  "
                  f"(±{s['expected_move_pct']}% / {f['horizon_days']}d, {s['mode']})  feeAPR={s['fee_apr_pct']}")
        else:
            print(f"  {s['pool']:<16} (range unavailable — {s['rationale']})")
