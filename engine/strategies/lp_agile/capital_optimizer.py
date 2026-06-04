"""engine/strategies/lp_agile/capital_optimizer.py — risk-adjusted capital
allocation across managed positions + chains.

Yomi 2026-05-30: tonight's goal is "fully automate the process including
optimising capital on each chain". This module is the SUGGEST layer; the
existing rebalance_executor handles the BUILD/SEND layer.

Approach (deterministic, no AI):
  1. For each open managed position:
       score = (realized_apr_pct OR projected_apr_pct) − chain_risk_premium
       weight = nav_usd · score
  2. For each chain (hyperevm, base, ...):
       compute target_weight per position; compare to current weight
       suggest: move capital from low-score → high-score within the same
                chain (cross-chain rebalances cost a bridge — flagged separately)
  3. Output proposals as 'add liquidity to X by $Y' / 'remove $Y from Z'.

Read sources:
  - managed_positions (registry)
  - ops/pwa/serve/lp_agile_latest.json (live state)
  - engine/_reports/lp_range_suggestions.json (projected APR if scanner ranked it)

Output:
  - engine/_signals/lp_capital_optimization.jsonl (append-only journal of proposals)
  - returns the dict for inspection

Knobs (env, all optional):
  LP_OPT_MIN_GAP_USD     default $10 — don't suggest moves below this size
  LP_OPT_PAYBACK_DAYS    default 7   — required payback days for cross-position moves
  LP_OPT_CHAIN_RISK      JSON dict of chain → premium pct, e.g. {"hyperevm": 5, "base": 0}
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.capital_optimizer")

_REPO_ROOT = Path(__file__).resolve().parents[3]
LP_LATEST = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_agile_latest.json"
RANGE_SUGGESTIONS = _REPO_ROOT / "engine" / "_reports" / "lp_range_suggestions.json"
PROPOSAL_LOG = _REPO_ROOT / "engine" / "_signals" / "lp_capital_optimization.jsonl"


MIN_GAP_USD = float(os.environ.get("LP_OPT_MIN_GAP_USD", "10"))
PAYBACK_DAYS = float(os.environ.get("LP_OPT_PAYBACK_DAYS", "7"))
try:
    CHAIN_RISK = json.loads(
        os.environ.get("LP_OPT_CHAIN_RISK") or '{"hyperevm": 5.0, "base": 0.0}'
    )
except Exception:
    CHAIN_RISK = {"hyperevm": 5.0, "base": 0.0}


# Default gas USD per move-of-capital event by chain. Conservative.
_MOVE_GAS_USD = {
    "hyperevm": 0.05,  # decreaseLiquidity + increaseLiquidity on prjx
    "base":     0.30,  # Slipstream — ~1-2 cents per tx, two txs
}


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _live_position_index() -> dict[int, dict]:
    """nft_token_id → live position dict."""
    data = _load_json(LP_LATEST, {})
    out = {}
    for p in (data.get("open_positions") or []):
        try:
            nft = int(p.get("nft_token_id") or 0)
            if nft:
                out[nft] = p
        except Exception:
            continue
    return out


def _expected_apr_for_pool(pool_address: str) -> Optional[float]:
    """Look up the scanner's projected APR for a pool (if it ranked it)."""
    data = _load_json(RANGE_SUGGESTIONS, {})
    ranked = data.get("suggestions") or data.get("ranked_pools") or []
    pa = pool_address.lower()
    for r in ranked:
        rp = (r.get("pool_address") or "").lower()
        if rp == pa:
            return (r.get("projected_apr_pct")
                    or r.get("expected_apr_pct")
                    or r.get("apr_pct"))
    return None


def _realized_apr_for_position(live_p: dict, managed_row: dict) -> Optional[float]:
    """Realised APR — prefer the canonical persisted value, fall back to a
    live-value inline estimate when the persisted column is NULL.

    Canonical source (2026-05-30, #171): managed_positions.realized_apr_pct,
    populated by managed_position.recompute_realized_apr() using
        (lifetime_fees − lifetime_gas) / avg_capital × (365.25 / days_open).

    Inline fallback (legacy estimator): fees-per-day divided by current
    live value_usd. Less accurate when capital has been added/removed, but
    keeps proposals flowing if the recompute hasn't run yet.
    """
    persisted = managed_row.get("realized_apr_pct")
    if persisted is not None:
        try:
            return float(persisted)
        except Exception:
            pass
    try:
        from datetime import datetime as _dt, timezone as _tz
        opened_at = managed_row.get("opened_at_iso")
        if not opened_at:
            return None
        opened = _dt.fromisoformat(opened_at.replace("Z", "+00:00"))
        days = max(
            (_dt.now(_tz.utc) - opened).total_seconds() / 86400.0, 0.5
        )
        value_usd = float(live_p.get("value_usd") or 0)
        if value_usd <= 0:
            return None
        lifetime_fees = float(managed_row.get("lifetime_fees_collected_usd") or 0)
        return (lifetime_fees / days) / value_usd * 365 * 100
    except Exception:
        return None


def score_position(*, managed_row: dict, live_p: dict) -> dict:
    """Compute a single risk-adjusted-APR score for a managed position."""
    chain = managed_row.get("chain") or "?"
    pool_addr = (managed_row.get("pool_address") or "").lower()
    nav_usd = float(live_p.get("value_usd") or 0)
    realized = _realized_apr_for_position(live_p, managed_row)
    projected = _expected_apr_for_pool(pool_addr)
    base_apr = realized if realized is not None else (projected or 0.0)
    chain_risk = float(CHAIN_RISK.get(chain) or 0.0)
    score = base_apr - chain_risk
    out = {
        "managed_position_id": managed_row["id"],
        "chain": chain,
        "pair": f"{managed_row.get('token0_symbol') or '?'}/"
                f"{managed_row.get('token1_symbol') or '?'}",
        "fee_tier": managed_row.get("fee_tier"),
        "nav_usd": round(nav_usd, 2),
        "realized_apr_pct": round(realized, 2) if realized else None,
        "projected_apr_pct": round(projected, 2) if projected else None,
        "chain_risk_premium": chain_risk,
        "score": round(score, 2),
        "score_weight": round(score * nav_usd, 2),
    }
    return out


def propose(*, write_journal: bool = True) -> dict:
    """Score every open managed position, then for each chain propose
    capital moves from low-scorers to high-scorers."""
    from engine.strategies.lp_agile import managed_position as MP
    managed = MP.list_managed_positions(status="open")
    live_idx = _live_position_index()
    scores = []
    for mp in managed:
        nft = mp.get("current_nft_token_id")
        live_p = live_idx.get(int(nft)) if nft is not None else None
        if not live_p:
            # try by pool address
            for p in live_idx.values():
                if (p.get("pool_address") or "").lower() == mp.get("pool_address"):
                    live_p = p
                    break
        if not live_p:
            continue
        scores.append(score_position(managed_row=mp, live_p=live_p))

    # Group by chain
    by_chain: dict[str, list[dict]] = {}
    for s in scores:
        by_chain.setdefault(s["chain"], []).append(s)

    proposals = []
    for chain, group in by_chain.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda s: s["score"], reverse=True)
        winner = group[0]
        for loser in group[1:]:
            score_gap = winner["score"] - loser["score"]
            if score_gap <= 0:
                continue
            # Move up to 50% of loser's NAV to winner if payback makes sense
            move_usd_max = round(loser["nav_usd"] * 0.5, 2)
            if move_usd_max < MIN_GAP_USD:
                continue
            move_gas_usd = _MOVE_GAS_USD.get(chain, 0.50)
            # Daily delta = (score_gap / 100 / 365) × move_usd
            delta_daily = (score_gap / 100 / 365) * move_usd_max
            payback_days = (
                move_gas_usd / delta_daily if delta_daily > 0 else None
            )
            triggered = (payback_days is not None
                         and payback_days <= PAYBACK_DAYS)
            proposals.append({
                "chain": chain,
                "from_managed_position_id": loser["managed_position_id"],
                "from_pair": loser["pair"],
                "to_managed_position_id": winner["managed_position_id"],
                "to_pair": winner["pair"],
                "move_usd": move_usd_max,
                "score_gap_pct": round(score_gap, 2),
                "est_gas_usd": move_gas_usd,
                "est_delta_daily_usd": round(delta_daily, 4),
                "payback_days": round(payback_days, 2) if payback_days else None,
                "triggered": triggered,
                "reason": (
                    f"score gap {score_gap:.1f}% — payback "
                    f"{payback_days:.1f}d ≤ {PAYBACK_DAYS}d"
                    if triggered else
                    f"score gap {score_gap:.1f}% — payback too slow"
                ),
            })

    summary = {
        "ts_iso": datetime.now(timezone.utc).isoformat(),
        "n_managed_positions": len(scores),
        "by_chain_counts": {k: len(v) for k, v in by_chain.items()},
        "scores": scores,
        "proposals": proposals,
        "n_triggered": sum(1 for p in proposals if p["triggered"]),
    }
    if write_journal:
        try:
            PROPOSAL_LOG.parent.mkdir(parents=True, exist_ok=True)
            with PROPOSAL_LOG.open("a") as f:
                f.write(json.dumps(summary, default=str) + "\n")
        except Exception as exc:
            logger.warning("[cap_opt] journal write failed: %s", exc)
    return summary


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(prog="capital_optimizer")
    ap.add_argument("--no-journal", action="store_true",
                    help="Do NOT append to the proposals journal")
    args = ap.parse_args()
    out = propose(write_journal=not args.no_journal)
    print(json.dumps(out, indent=2, default=str))
