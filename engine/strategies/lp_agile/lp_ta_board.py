"""engine/strategies/lp_agile/lp_ta_board.py — TA range advisory board.

Turns lp_market_view into a per-position, see-it-on-the-PWA board: for each open
LP position it shows the market view (bias/confidence), the TA-suggested range (or
a HOLD verdict), and how far that suggestion has drifted from the current range
centre. ADVISORY ONLY — nothing executes. On Base, a suggestion whose centre drifts
> LP_TA_BASE_DRIFT_PCT (5%) from the current centre AND clears the confidence floor
is tagged 'auto_candidate' (what *would* auto-rebalance once the backtest passes +
the re-mint signer is live); on HyperEVM everything is 'advisory'.

Pure given an injected view_fn → unit-tested. Publishes ops/pwa/serve/lp_ta_view.json.
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import lp_market_view as MV

logger = logging.getLogger("engine.strategies.lp_agile.lp_ta_board")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REPORT = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_report.json"
BOARD_OUT = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_ta_view.json"

_STABLES = {"USDC", "USDT", "DAI", "USD", "USDHL", "USDT0", "FEUSD", "USDXL", "USDBC"}


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _view_asset(pair: str) -> tuple:
    """Pick the leg to run TA on: the non-stable (volatile) token. Returns
    (asset, approx) where approx=True for volatile/volatile pairs (ratio proxy)."""
    legs = [p for p in (pair or "").replace("-", "/").split("/") if p]
    legs_up = [l.upper() for l in legs]
    nonstable = [l for l in legs_up if l not in _STABLES]
    if len(nonstable) == 1:
        return nonstable[0], False           # stable-quoted: token-USD == pair price
    if len(nonstable) >= 2:
        return nonstable[0], True            # vol/vol: proxy on base leg (note approx)
    return (legs_up[0] if legs_up else "?"), False  # stable/stable


def _geom_center(lo, hi):
    try:
        lo, hi = float(lo), float(hi)
        if lo > 0 and hi > 0:
            return math.sqrt(lo * hi)
    except Exception:
        pass
    return None


def build_board(positions: list, *, view_fn: Callable, horizon_days: int = 30) -> dict:
    """PURE given view_fn(asset)->view dict. One row per position."""
    base_drift = _f("LP_TA_BASE_DRIFT_PCT", 5.0)
    conf_floor = _f("LP_TA_CONFIDENCE_FLOOR", 0.55)
    rows = []
    for p in positions:
        pair = p.get("pair")
        chain = (p.get("chain") or "").lower()
        price = p.get("price_now") or p.get("pool_price_now")
        lo, hi = p.get("range_low"), p.get("range_high")
        asset, approx = _view_asset(pair)
        view = view_fn(asset) or {}
        row = {"pair": pair, "chain": chain, "view_asset": asset,
               "approx_ratio_proxy": approx, "bias": view.get("bias"),
               "confidence": view.get("confidence"), "mode": "advisory"}
        if not price:
            row.update({"action": "no_price", "reason": "no live pool price"})
            rows.append(row)
            continue
        rng = MV.recommend_range(float(price), view, horizon_days=horizon_days)
        row["action"] = rng["action"]
        row["reason"] = rng["reason"]
        row["suggested"] = {k: rng.get(k) for k in ("center", "low", "high", "skew", "source",
                                                     "expected_move_pct", "sigma_horizon_pct")}
        cur_center = _geom_center(lo, hi)
        row["current_center"] = round(cur_center, 8) if cur_center else None
        if cur_center and rng.get("center"):
            drift = (rng["center"] - cur_center) / cur_center * 100.0
            row["center_drift_pct"] = round(drift, 1)
            # Base auto-candidate flag (advisory until backtest + signer live)
            strong = abs(drift) > base_drift and (view.get("confidence") or 0) >= conf_floor
            if chain == "base" and rng["action"] == "lp" and rng.get("source") == "ta" and strong:
                row["mode"] = "auto_candidate"
        rows.append(row)

    notifs = []
    for r in rows:
        if r.get("action") == "hold":
            notifs.append({"chain": r["chain"], "pair": r["pair"], "severity": "medium",
                           "event": f"{r['pair']}: TA says HOLD (strong {r.get('bias')}).",
                           "action": r["reason"], "mode": r["mode"]})
        elif r.get("mode") == "auto_candidate":
            notifs.append({"chain": r["chain"], "pair": r["pair"], "severity": "medium",
                           "event": f"{r['pair']}: TA range drifted {r.get('center_drift_pct')}% — re-centre candidate.",
                           "action": f"suggested {r['suggested'].get('skew')}-skew range; "
                                     f"would auto-rebalance on Base once validated.",
                           "mode": r["mode"]})
    return {
        "generated_at_iso": datetime.now(timezone.utc).isoformat(),
        "advisory_only": True, "horizon_days": horizon_days,
        "base_drift_threshold_pct": base_drift, "confidence_floor": conf_floor,
        "positions": rows, "notifications": notifs,
    }


def run(*, write: bool = True, horizon_days: int = 30) -> dict:
    try:
        report = json.loads(_REPORT.read_text())
        positions = report.get("positions") or []
    except Exception:
        positions = []
    board = build_board(positions, view_fn=MV.view_for_asset, horizon_days=horizon_days)
    if write:
        try:
            BOARD_OUT.parent.mkdir(parents=True, exist_ok=True)
            tmp = BOARD_OUT.with_suffix(".tmp")
            tmp.write_text(json.dumps(board, indent=2, default=str))
            tmp.replace(BOARD_OUT)
            logger.info("[lp_ta_board] %d positions → %s", len(board["positions"]), BOARD_OUT)
        except Exception as exc:                                      # noqa: BLE001
            logger.error("[lp_ta_board] publish failed: %s", exc)
    return board


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="TA range advisory board")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--horizon", type=int, default=30)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(json.dumps(run(write=not args.no_write, horizon_days=args.horizon), indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
