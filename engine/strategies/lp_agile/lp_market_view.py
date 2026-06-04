"""engine/strategies/lp_agile/lp_market_view.py — TA-driven LP range view.

Today's range_optimizer centres a SYMMETRIC ±2σ band on spot using STATIC per-
asset sigma guesses, with zero directional view — the likely reason the LP bleeds
(blind ranges). This module adds a proper market read per token and turns it into
a range, or — when conviction is high — a "don't LP, hold" verdict (concentrated
LP sells the winner into strength = IL; sometimes spot is simply better).

Pure core (compute_view, range_from_view) → unit-testable on synthetic series.
The live adapter (view_for_asset) reuses engine.strategies.md_ai.enrichment.
crypto_signals (ATR, returns, RS-vs-BTC; operator-side, needs network). ONE market
brain shared with the perps side — not a second TA engine.

Honesty: TA does not predict a price a month out. It predicts a *cone* (vol band
P·e^(±σ√T)) + a directional *bias* + a *confidence*. The range is the cone, skewed
by drift. Strong drift relative to vol → hold instead of LP.
"""
from __future__ import annotations

import logging
import math
import os
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.lp_market_view")


def _log_returns(closes: list) -> list:
    out = []
    for a, b in zip(closes[:-1], closes[1:]):
        if a and b and a > 0 and b > 0:
            out.append(math.log(b / a))
    return out


def _stdev(xs: list) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _ema(xs: list, span: int) -> Optional[float]:
    if not xs:
        return None
    k = 2.0 / (span + 1)
    e = xs[0]
    for x in xs[1:]:
        e = x * k + e * (1 - k)
    return e


def compute_view(closes: list) -> dict:
    """PURE: daily-bar closes → {bias, drift_daily, sigma_daily, confidence, n}.

    bias from EMA-fast-vs-slow + drift sign; confidence from signal-to-noise
    (|drift|/sigma over the horizon proxy) clamped to [0,1]. No network."""
    closes = [float(c) for c in closes if c]
    n = len(closes)
    if n < 8:
        return {"bias": "neutral", "drift_daily": 0.0, "sigma_daily": 0.0,
                "confidence": 0.0, "n": n, "note": "insufficient history"}
    rets = _log_returns(closes)
    sigma = _stdev(rets)
    drift = sum(rets) / len(rets)
    ema_fast = _ema(closes, max(2, n // 6))
    ema_slow = _ema(closes, max(4, n // 2))
    trend_up = ema_fast is not None and ema_slow is not None and ema_fast > ema_slow
    trend_dn = ema_fast is not None and ema_slow is not None and ema_fast < ema_slow

    # signal-to-noise over a ~30-bar horizon: |cumulative drift| vs 1σ move
    horizon = min(30, n)
    snr = abs(drift * horizon) / (sigma * math.sqrt(horizon)) if sigma > 0 else 0.0
    confidence = max(0.0, min(1.0, snr))  # ~ t-stat-ish, capped

    if drift > 0 and trend_up:
        bias = "up"
    elif drift < 0 and trend_dn:
        bias = "down"
    else:
        bias = "neutral"
        confidence *= 0.5   # mixed signals → discount
    return {"bias": bias, "drift_daily": round(drift, 6),
            "sigma_daily": round(sigma, 6), "confidence": round(confidence, 3),
            "n": n}


def range_from_view(price: float, view: dict, *, horizon_days: int = 30,
                    sigma_mult: float = 2.0) -> dict:
    """PURE: market view → drift-skewed cone range OR a 'hold' verdict.

    Cone: centre = price·e^(drift·T); band = centre·e^(±mult·σ·√T). Drift moves the
    centre, so the range auto-skews in the bias direction. When the expected move
    (|drift·T|) exceeds ~1σ AND confidence is high, concentrated LP would bleed IL
    on the trend → recommend HOLD (spot / very-wide) instead."""
    hold_conf = float(os.environ.get("LP_TA_HOLD_CONFIDENCE", "0.7"))
    sigma = float(view.get("sigma_daily") or 0.0)
    drift = float(view.get("drift_daily") or 0.0)
    conf = float(view.get("confidence") or 0.0)
    sigma_h = sigma * math.sqrt(horizon_days)
    drift_h = drift * horizon_days

    if sigma_h <= 0:
        return {"action": "lp", "reason": "no vol estimate — fall back to optimizer default",
                "center": price, "low": None, "high": None, "skew": "none", "view": view}

    # LP-vs-HOLD gate: strong directional conviction → don't LP into the trend.
    if conf >= hold_conf and abs(drift_h) >= sigma_h:
        return {"action": "hold", "reason":
                (f"strong {view.get('bias')} conviction (conf {conf:.2f}, expected move "
                 f"{drift_h*100:+.0f}% > 1σ {sigma_h*100:.0f}%) — concentrated LP would "
                 f"bleed IL; hold spot or go very wide"),
                "center": round(price * math.exp(drift_h), 8), "view": view}

    center = price * math.exp(drift_h)
    low = center * math.exp(-sigma_mult * sigma_h)
    high = center * math.exp(+sigma_mult * sigma_h)
    skew = "up" if drift_h > 0.01 else "down" if drift_h < -0.01 else "symmetric"
    return {"action": "lp", "reason": f"{skew}-skewed {sigma_mult:.0f}σ cone over {horizon_days}d",
            "center": round(center, 8), "low": round(low, 8), "high": round(high, 8),
            "skew": skew, "sigma_horizon_pct": round(sigma_h * 100, 1),
            "expected_move_pct": round(drift_h * 100, 1), "confidence": conf, "view": view}


def recommend_range(price: float, view: dict, *, sym_width: float = 0.10,
                    conf_floor: Optional[float] = None, horizon_days: int = 30,
                    sigma_mult: float = 2.0) -> dict:
    """HYBRID ranger (backtest-validated): default to a symmetric +/-sym_width band;
    only deviate to the TA cone/HOLD when confidence >= conf_floor. This is the
    policy that beat pure-symmetric in trend AND chop (pure-TA over-traded chop).
    Worst case approx symmetric; upside = dodging the IL trap on real trends."""
    if conf_floor is None:
        conf_floor = float(os.environ.get("LP_TA_HYBRID_CONF_FLOOR", "0.7"))
    if (view.get("confidence") or 0) >= conf_floor:
        r = range_from_view(price, view, horizon_days=horizon_days, sigma_mult=sigma_mult)
        r["source"] = "ta"
        return r
    return {"action": "lp", "source": "symmetric", "center": round(price, 8),
            "low": round(price * (1 - sym_width), 8), "high": round(price * (1 + sym_width), 8),
            "skew": "symmetric",
            "reason": f"low conviction (conf {view.get('confidence')}) — default ±{sym_width*100:.0f}% range"}


def view_for_asset(asset: str, *, closes: Optional[list] = None) -> dict:
    """Live adapter (operator-side). Uses provided closes, else pulls a return
    series from md_ai crypto_signals. Never raises → neutral view on failure."""
    if closes is None:
        try:
            from engine.strategies.md_ai.enrichment import crypto_signals as cs
            sig = cs.get_per_asset_signals(asset) or {}
            # crypto_signals exposes recent log_returns; reconstruct a pseudo-series
            rets = sig.get("log_returns") or sig.get("returns_30d") or []
            if rets:
                px = [100.0]
                for r in rets:
                    px.append(px[-1] * math.exp(float(r)))
                closes = px
        except Exception as exc:                                      # noqa: BLE001
            logger.debug("[market_view] crypto_signals unavailable for %s: %s", asset, exc)
    if not closes:
        return {"asset": asset, **compute_view([])}
    return {"asset": asset, **compute_view(closes)}
