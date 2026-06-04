"""engine/strategies/lp_agile/pwa_snapshot.py — JSON writer for PWA LP card.

Per Yomi 2026-05-23: PWA needs read-only LP card (no APPROVE button).
Emits lp_agile_latest.json that the PWA polls every refresh.

Schema:
{
  "generated_at_iso": "...",
  "ranked_pools": [
    {"id", "protocol", "chain", "pair", "fee_apr_pct", "tvl_usd",
     "score", "verdict", "is_top": bool}
  ],
  "open_positions": [
    {"nft_token_id", "pool_id", "pair", "in_range", "value_usd",
     "fees_owed_usd", "range_low", "range_high"}
  ],
  "ledger_summary": {
    "n_positions_ever", "total_gas_usd", "total_benefits_usd",
    "total_net_pnl_usd"
  },
  "config": {
    "wallet_address", "auto_execute_enabled",
    "per_position_max_usd", "target_apr_pct"
  }
}
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.pwa_snapshot")

SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[3]
    / "ops" / "pwa" / "serve" / "lp_agile_latest.json"
)


def _d(x) -> float:
    """Decimal → float for JSON."""
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def build_snapshot(*, wallet_address: Optional[str] = None) -> dict:
    """Build the full LP-agile snapshot dict — runs scan + reads wallet + ledger."""
    from engine.data.lp_pools import get_adapter, list_registered
    from engine.strategies.lp_agile.ai_judge import judge_and_annotate
    from engine.strategies.lp_agile.cost_ledger import summarize_all_positions
    from engine.strategies.lp_agile.env import get_lp_config
    from engine.strategies.lp_agile.ranker import rank_pools
    from engine.strategies.lp_agile.triggers import evaluate_triggers
    from engine.strategies.lp_agile.types import LPAction
    from engine.strategies.lp_agile.universe import load_pool_universe
    from engine.strategies.lp_agile.wallet import read_lp_nft_positions
    from engine.strategies.lp_agile.wallet_staked import read_staked_positions

    cfg = load_pool_universe()
    snapshots = []
    for pool in cfg.pools:
        try:
            s = get_adapter(pool.protocol).fetch_snapshot(pool)
            if s is not None:
                snapshots.append(s)
        except Exception as e:                            # noqa: BLE001
            logger.warning("snapshot fetch failed for %s: %s", pool.id, e)

    ranked = rank_pools(snapshots, weights=cfg.weights,
                        freshness_max_s=cfg.freshness_max_s)

    # Run AI judge on top OPEN signal so PWA shows verdict (cheap — rule-tier only on misses)
    judged_verdict = {}
    signals = evaluate_triggers(ranked)
    open_signals = [s for s in signals if s.action == LPAction.OPEN]
    for s in open_signals[:3]:
        try:
            js = judge_and_annotate(s)
            judged_verdict[s.pool.id] = {
                "verdict": js.ai_judge_verdict,
                "reasoning": js.ai_judge_reasoning,
                "tier": js.ai_judge_tier,
            }
        except Exception as e:                            # noqa: BLE001
            logger.info("judge failed for %s: %s", s.pool.id, e)

    ranked_pools = []
    top_id = ranked[0].snapshot.pool.id if ranked else None
    for r in ranked[:10]:
        pid = r.snapshot.pool.id
        ranked_pools.append({
            "id": pid,
            "protocol": r.snapshot.pool.protocol.value,
            "chain": r.snapshot.pool.chain.value,
            "pair": r.snapshot.pool.pair,
            "pool_address": r.snapshot.pool.pool_address,
            "fee_tier_bps": r.snapshot.pool.fee_tier_bps,
            "fee_apr_pct": _d(r.snapshot.fee_apr) * 100 if r.snapshot.fee_apr else None,
            "tvl_usd": _d(r.snapshot.tvl_usd),
            "volume_24h_usd": _d(r.snapshot.volume_24h_usd),
            "current_price_usd": _d(r.snapshot.base_price_usd),
            "score": _d(r.score),
            "rationale": r.rationale,
            "is_top": pid == top_id,
            "verdict": judged_verdict.get(pid, {}).get("verdict"),
            "verdict_tier": judged_verdict.get(pid, {}).get("tier"),
            "verdict_reasoning": judged_verdict.get(pid, {}).get("reasoning"),
        })

    # Wallet positions — both wallet-held AND staked (gauge-held). The
    # wallet-held path misses staked NFTs (ownership transferred to gauge);
    # read_staked_positions covers that gap by enumerating gauge.stakedValues().
    # De-duplicated by (token_id, pool_address) in case both readers ever
    # return the same NFT.
    open_positions: list[dict] = []
    seen_keys: set[tuple] = set()
    try:
        wallet = wallet_address
        if not wallet:
            lp_cfg = get_lp_config()
            wallet = lp_cfg.get("wallet_address")
        if wallet:
            for proto in list_registered():
                try:
                    adapter = get_adapter(proto)
                    # 1) Wallet-held NFTs (balanceOf + tokenOfOwnerByIndex)
                    held = read_lp_nft_positions(adapter, wallet)
                    # 2) Staked NFTs (gauge.stakedValues) — Slipstream only
                    staked = read_staked_positions(adapter, wallet, cfg.all_pools)
                    for p in (list(held) + list(staked)):
                        key = (p.token_id, p.pool_address.lower())
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        open_positions.append({
                            "nft_token_id": p.token_id,
                            "protocol": p.protocol,
                            "pool_address": p.pool_address,
                            "pair": f"{p.token0_symbol}/{p.token1_symbol}",
                            "in_range": p.in_range,
                            "value_usd": _d(p.position_value_usd),
                            "fees_owed_usd": _d(p.fees_owed_value_usd),
                            "range_low": _d(p.price_lower),
                            "range_high": _d(p.price_upper),
                            "pool_price_now": _d(p.pool_price),
                            "staked": bool(p.staked),
                            "staked_in_gauge": p.staked_in_gauge,
                            "pending_aero": _d(p.pending_aero),
                            "pending_aero_usd": _d(p.pending_aero_usd),
                        })
                except Exception as e:                    # noqa: BLE001
                    logger.info("wallet positions read failed for %s: %s",
                                proto.value, e)
    except Exception as e:                                # noqa: BLE001
        logger.info("wallet config read failed: %s", e)

    # Cost ledger summary
    ledger_summary = {
        "n_positions_ever": 0,
        "total_gas_usd": 0.0,
        "total_other_costs_usd": 0.0,
        "total_benefits_usd": 0.0,
        "total_net_pnl_usd": 0.0,
    }
    try:
        summaries = [s for s in summarize_all_positions() if s is not None]
        ledger_summary["n_positions_ever"] = len(summaries)
        ledger_summary["total_gas_usd"] = sum(_d(s.total_gas_usd) for s in summaries)
        ledger_summary["total_other_costs_usd"] = sum(_d(s.total_other_costs_usd) for s in summaries)
        ledger_summary["total_benefits_usd"] = sum(_d(s.total_benefits_usd) for s in summaries)
        ledger_summary["total_net_pnl_usd"] = sum(_d(s.net_pnl_usd) for s in summaries)
    except Exception as e:                                # noqa: BLE001
        logger.info("ledger summary read failed: %s", e)

    # Config slice (no secrets)
    try:
        lp_cfg = get_lp_config()
        config = {
            "wallet_address": lp_cfg["wallet_address"],
            "auto_execute_enabled": lp_cfg["auto_execute"],
            "per_position_max_usd": _d(lp_cfg["per_position_max_usd"]),
            "total_bankroll_usd": _d(lp_cfg["total_bankroll_usd"]),
            "target_apr_pct": _d(lp_cfg["target_apr_pct"]),
            "start_chain": lp_cfg["start_chain"],
            "start_pool": lp_cfg["start_pool"],
        }
    except Exception:
        config = {"wallet_address": None, "auto_execute_enabled": False,
                  "per_position_max_usd": 10.0, "target_apr_pct": 100.0}

    return {
        "generated_at_iso": datetime.now(timezone.utc).isoformat(),
        "strategy_id": "lp_agile_subscriber_v1",
        "ranked_pools": ranked_pools,
        "open_positions": open_positions,
        "ledger_summary": ledger_summary,
        "config": config,
    }


def write_snapshot(*, path: Optional[Path] = None,
                   wallet_address: Optional[str] = None) -> dict:
    """Build + write snapshot atomically. Returns the written dict.

    Also triggers a position-level rebalance plan refresh (cheap, in-process)
    so PWA always sees fresh plan suggestions tied to the latest snapshot.
    Failure to refresh plans does NOT block the snapshot write.
    """
    snap = build_snapshot(wallet_address=wallet_address)
    target = path or SNAPSHOT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, indent=2, default=str))
    tmp.replace(target)
    logger.info("wrote LP snapshot to %s (%d pools, %d positions)",
                target, len(snap["ranked_pools"]), len(snap["open_positions"]))

    # Refresh rebalance plans tied to this snapshot (Phase 2a suggestion-only).
    try:
        from engine.strategies.lp_agile.rebalance_plan import run_once as _reb_run
        _reb_run(wallet_address=wallet_address)
    except Exception as exc:                                  # noqa: BLE001
        logger.info("rebalance plan refresh failed (non-blocking): %s", exc)

    return snap
