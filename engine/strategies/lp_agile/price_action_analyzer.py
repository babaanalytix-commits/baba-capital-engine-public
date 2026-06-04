"""engine/strategies/lp_agile/price_action_analyzer.py — directional
view on LP pair price action.

Yomi 2026-06-04 #420: "We can optimise returns by constantly monitoring
price action of both pairs on any LP, just like we are on directional
trades. This will help give us an edge regarding the optimal range,
then we can periodically or dynamically adjust the range to optimise
returns, not necessarily waiting till it gets out of range."

Pulls recent OHLCV candles for the underlying pair token (e.g., HYPE/USD
for WHYPE/USDC) from Hyperliquid's candleSnapshot, computes deterministic
indicators, returns a structured directional view:

  - trend_direction: 'up' | 'down' | 'sideways'
  - expected_drift_pct_24h: signed % move expected over next 24h
  - vol_pct_24h: realized vol (annualised → 24h scaled)
  - regime: 'trending' | 'mean_reverting' | 'choppy'
  - confidence: 0.0 - 1.0 based on signal alignment

Why deterministic (not AI):
  - Runs every 10-30 min on HyperEVM → AI cost prohibitive
  - Indicators are well-understood and cheap to compute
  - Reproducible / unit-testable
  - We already have MD AI for full directional trading decisions; this
    is just an enrichment for the range planner

Standing rule: this module is PURE given candle inputs. Network reads live
in the fetcher only — easy to mock.
"""
from __future__ import annotations

import logging
import math
import os
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.price_action_analyzer")

_REPO = Path(__file__).resolve().parents[3]


@dataclass
class PriceActionView:
    """Structured directional view for a pair."""
    symbol: str                  # e.g., "HYPE"
    current_price_usd: float
    trend_direction: str         # 'up' | 'down' | 'sideways'
    expected_drift_pct_24h: float  # signed (+up / -down) over next 24h
    vol_pct_24h: float           # 1σ daily range, e.g., 0.05 = ±5%
    regime: str                  # 'trending' | 'mean_reverting' | 'choppy'
    confidence: float            # 0.0 - 1.0
    raw: dict                    # debug payload

    @property
    def expected_low_pct(self) -> float:
        """Expected lower bound (% of current price) over next 24h.

        Centered on drift + vol: drift − 2σ.
        Example: drift=−2%, vol=4% → expected_low_pct = −10%
        """
        return self.expected_drift_pct_24h - 2.0 * self.vol_pct_24h * 100

    @property
    def expected_high_pct(self) -> float:
        """Expected upper bound (% of current price) over next 24h.

        drift + 2σ.
        """
        return self.expected_drift_pct_24h + 2.0 * self.vol_pct_24h * 100


def _fetch_hl_candles(coin: str, interval: str = "1h", lookback_h: int = 168) -> list[dict]:
    """Pull recent candles from HL candleSnapshot. 168h = 7 days of 1h candles.

    Returns list of {t, o, h, l, c, v} dicts; empty list on failure.
    """
    import urllib.request
    import json
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - lookback_h * 3_600_000
    body = json.dumps({
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval,
                "startTime": start_ms, "endTime": now_ms},
    }).encode()
    req = urllib.request.Request(
        "https://api.hyperliquid.xyz/info",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode())
        if not isinstance(data, list):
            return []
        # HL candle format: {"t": ms, "T": end_ms, "o": "...", "h": "...",
        #   "l": "...", "c": "...", "v": "...", "n": int, "i": interval}
        return [
            {
                "t": int(c["t"]),
                "o": float(c["o"]),
                "h": float(c["h"]),
                "l": float(c["l"]),
                "c": float(c["c"]),
                "v": float(c.get("v", 0) or 0),
            }
            for c in data
        ]
    except Exception as exc:                                      # noqa: BLE001
        logger.warning("HL candleSnapshot failed for %s: %s", coin, exc)
        return []


def _ema(values: list[float], period: int) -> Optional[float]:
    """Exponential moving average. Returns None if insufficient data."""
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def _atr_pct(candles: list[dict], period: int = 14) -> Optional[float]:
    """Average True Range as % of current price."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        c = candles[i]
        prev = candles[i - 1]
        tr = max(
            c["h"] - c["l"],
            abs(c["h"] - prev["c"]),
            abs(c["l"] - prev["c"]),
        )
        trs.append(tr)
    # Use last `period` TRs
    atr = sum(trs[-period:]) / period
    current = candles[-1]["c"]
    return atr / current if current > 0 else None


def _realised_vol_pct_daily(candles_1h: list[dict]) -> Optional[float]:
    """Annualised realised vol from 1h closes, scaled to 1 day.

    σ_daily = σ_hourly × √24
    """
    if len(candles_1h) < 24:
        return None
    closes = [c["c"] for c in candles_1h[-72:]]  # last 72h
    log_returns = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i - 1] > 0
    ]
    if len(log_returns) < 12:
        return None
    sigma_hourly = statistics.stdev(log_returns)
    return sigma_hourly * math.sqrt(24)


def analyze(symbol: str, *, candles_1h: Optional[list[dict]] = None) -> Optional[PriceActionView]:
    """Compute the directional view for `symbol` (e.g., 'HYPE', 'BTC').

    Args:
      symbol: HL coin name. WHYPE LPs → 'HYPE'. cbBTC/UBTC LPs → 'BTC'.
      candles_1h: optional pre-fetched candles (for testing / batching).
                  When None, fetches from HL.

    Returns: PriceActionView or None if data insufficient.
    """
    if candles_1h is None:
        candles_1h = _fetch_hl_candles(symbol, interval="1h", lookback_h=168)
    if not candles_1h or len(candles_1h) < 24:
        return None

    closes = [c["c"] for c in candles_1h]
    current_price = closes[-1]

    # Trend: EMA(20h) vs current + EMA(50h) vs EMA(20h)
    ema_20 = _ema(closes, 20)
    ema_50 = _ema(closes, 50)
    if ema_20 is None or ema_50 is None:
        return None

    # Volatility: realised daily vol from 72h of 1h closes
    vol_daily = _realised_vol_pct_daily(candles_1h)
    if vol_daily is None:
        vol_daily = _atr_pct(candles_1h, 14) or 0.05

    # Trend direction
    trend_strength = (current_price - ema_20) / ema_20  # signed pct
    trend_alignment = (ema_20 - ema_50) / ema_50        # signed pct

    if abs(trend_strength) < 0.005 and abs(trend_alignment) < 0.005:
        trend_direction = "sideways"
    elif trend_strength > 0 and trend_alignment > 0:
        trend_direction = "up"
    elif trend_strength < 0 and trend_alignment < 0:
        trend_direction = "down"
    else:
        trend_direction = "sideways"

    # Regime: trending if both EMAs aligned + above vol noise
    # Mean reverting if price > 2σ from EMA20
    # Choppy if neither
    deviation_from_ema = abs(current_price - ema_20) / ema_20
    if deviation_from_ema > 2 * vol_daily / math.sqrt(24):  # 2σ hourly
        regime = "mean_reverting"
    elif (trend_strength * trend_alignment) > 0 and abs(trend_strength) > vol_daily / math.sqrt(24):
        regime = "trending"
    else:
        regime = "choppy"

    # Expected drift over next 24h
    # Lean on trend signal but moderate by regime confidence
    if trend_direction == "up":
        expected_drift_pct = min(trend_strength * 100, vol_daily * 100)
    elif trend_direction == "down":
        expected_drift_pct = max(trend_strength * 100, -vol_daily * 100)
    else:
        expected_drift_pct = 0.0

    # Confidence: stronger when trend + alignment same sign + away from noise
    if regime == "trending":
        confidence = min(1.0, abs(trend_alignment) / max(vol_daily, 0.01) * 2)
    elif regime == "mean_reverting":
        confidence = min(0.7, deviation_from_ema / max(vol_daily / 4, 0.005))
    else:
        confidence = 0.3

    return PriceActionView(
        symbol=symbol,
        current_price_usd=current_price,
        trend_direction=trend_direction,
        expected_drift_pct_24h=round(expected_drift_pct, 3),
        vol_pct_24h=round(vol_daily, 4),
        regime=regime,
        confidence=round(confidence, 3),
        raw={
            "ema_20": round(ema_20, 4),
            "ema_50": round(ema_50, 4),
            "trend_strength_pct": round(trend_strength * 100, 3),
            "trend_alignment_pct": round(trend_alignment * 100, 3),
            "candles_used": len(candles_1h),
        },
    )


# Map LP pair → which HL coin to analyze
PAIR_TO_HL_SYMBOL = {
    "WHYPE/USDC": "HYPE",
    "USDC/WHYPE": "HYPE",
    "WHYPE/UBTC": "HYPE",   # primarily HYPE-driven; UBTC ≈ stable BTC ratio
    "UBTC/WHYPE": "HYPE",
    "USDC/CBBTC": "BTC",
    "CBBTC/USDC": "BTC",
    "WETH/USDC": "ETH",
    "WETH/CBBTC": "ETH",   # ETH-driven over short horizons
}


def analyze_lp_pair(pair: str) -> Optional[PriceActionView]:
    """Analyze the underlying asset for an LP pair label like 'WHYPE/USDC'."""
    sym = PAIR_TO_HL_SYMBOL.get(pair.upper())
    if not sym:
        logger.info("No HL symbol mapping for pair %s", pair)
        return None
    return analyze(sym)


if __name__ == "__main__":
    import json, sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sym = sys.argv[1] if len(sys.argv) > 1 else "HYPE"
    view = analyze(sym)
    if view is None:
        print(f"No data for {sym}")
    else:
        print(json.dumps({
            "symbol": view.symbol,
            "current_price_usd": view.current_price_usd,
            "trend_direction": view.trend_direction,
            "expected_drift_pct_24h": view.expected_drift_pct_24h,
            "vol_pct_24h": view.vol_pct_24h,
            "regime": view.regime,
            "confidence": view.confidence,
            "expected_low_pct": view.expected_low_pct,
            "expected_high_pct": view.expected_high_pct,
            "raw": view.raw,
        }, indent=2))
