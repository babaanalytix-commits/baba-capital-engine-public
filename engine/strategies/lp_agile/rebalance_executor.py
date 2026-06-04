"""engine/strategies/lp_agile/rebalance_executor.py — Phase 2 of LP pivot.

Reads triggered rebalances from `lp_rebalance_triggers.jsonl` (written by
rebalance_trigger.py) and turns each into an IN-PLACE mutation plan via the
appropriate chain adapter:
  - HyperEVM / prjx       → prjx_adapter.rebalance_in_place
  - Base / Aerodrome      → aerodrome_adapter (not yet built — task #162)

Per Yomi 2026-05-30 LP architecture pivot: we track ONE logical position per
(chain, pool, fee_tier) in `managed_positions`. Each rebalance is a MUTATION
on that logical position, not a new position. The underlying NFT tokenId
changes (Uniswap V3 NFTs have immutable ticks), but the logical position
persists with lifetime metrics rolling up.

Live tx-send is NOT wired in V1 — the executor builds + records the PLAN, but
sending requires the wallet signer integration (task #160). Until then, this
runs as a planner/auditor: every triggered rebalance gets a fully-formed
plan persisted to `mutations` for review.

Usage:
    cd ~/baba/wealth-ecosystem && \\
        python3 -m engine.strategies.lp_agile.rebalance_executor run-once
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.rebalance_executor")

_REPO_ROOT = Path(__file__).resolve().parents[3]
TRIGGER_QUEUE = _REPO_ROOT / "engine" / "_signals" / "lp_rebalance_triggers.jsonl"
PROCESSED_LEDGER = _REPO_ROOT / "engine" / "_signals" / "lp_rebalance_processed.jsonl"
LP_LATEST = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_agile_latest.json"


def _read_pending_triggers() -> list[dict]:
    """Triggers that haven't been processed yet. Track via processed ledger
    (signal_id-equivalent = enqueued_at_iso + position_id)."""
    if not TRIGGER_QUEUE.exists():
        return []
    seen_keys = set()
    if PROCESSED_LEDGER.exists():
        try:
            for ln in PROCESSED_LEDGER.read_text().splitlines():
                if not ln.strip():
                    continue
                try:
                    r = json.loads(ln)
                    seen_keys.add(r.get("trigger_key"))
                except Exception:
                    continue
        except Exception:
            pass
    out = []
    try:
        for ln in TRIGGER_QUEUE.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                t = json.loads(ln)
            except Exception:
                continue
            key = f"{t.get('enqueued_at_iso')}|{t.get('position_id')}"
            if key in seen_keys:
                continue
            t["_trigger_key"] = key
            out.append(t)
    except Exception as exc:
        logger.warning("[rebal_exec] failed to read queue: %s", exc)
    return out


def _mark_processed(trigger_key: str, *, action: str,
                     outcome: str, detail: Optional[dict] = None) -> None:
    try:
        PROCESSED_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with PROCESSED_LEDGER.open("a") as f:
            f.write(json.dumps({
                "processed_at_iso": datetime.now(timezone.utc).isoformat(),
                "trigger_key": trigger_key,
                "action": action,
                "outcome": outcome,
                "detail": detail or {},
            }, default=str) + "\n")
    except Exception as exc:
        logger.warning("[rebal_exec] processed-ledger write failed: %s", exc)


def _lookup_position_state(position_id: str) -> Optional[dict]:
    """Find the position's current on-chain state in lp_positions_latest.json.
    position_id format: '<pool_address>|<nft_token_id>'."""
    if not LP_LATEST.exists():
        return None
    try:
        data = json.loads(LP_LATEST.read_text())
    except Exception:
        return None
    pool_addr, _, nft = position_id.partition("|")
    pool_addr = pool_addr.lower()
    for p in data.get("positions") or []:
        pp = (p.get("pool_address") or "").lower()
        tid = str(p.get("nft_token_id") or p.get("token_id") or "")
        if pp == pool_addr and tid == nft:
            return p
    return None


def _propose_new_ticks(position: dict) -> tuple[Optional[int], Optional[int]]:
    """Heuristic: re-center the range on the current pool tick.
    Width preserved from current range. Returns (new_tick_lower, new_tick_upper)."""
    cur_tick = position.get("current_pool_tick")
    cur_lower = position.get("tick_lower")
    cur_upper = position.get("tick_upper")
    if cur_tick is None or cur_lower is None or cur_upper is None:
        return None, None
    try:
        cur_tick = int(cur_tick)
        cur_lower = int(cur_lower)
        cur_upper = int(cur_upper)
    except Exception:
        return None, None
    width = cur_upper - cur_lower
    half = width // 2
    new_lower = cur_tick - half
    new_upper = cur_tick + half
    # Snap to tick spacing — fee 3000 → spacing 60; fee 500 → 10; fee 100 → 1
    fee_tier = int(position.get("fee_tier") or position.get("fee") or 3000)
    spacing = {100: 1, 500: 10, 3000: 60, 10000: 200}.get(fee_tier, 60)
    new_lower -= (new_lower % spacing)
    new_upper -= (new_upper % spacing)
    return new_lower, new_upper


def _build_plan_for_trigger(trigger: dict) -> dict:
    """Look up position state, propose new ticks, build calldata plan."""
    from engine.strategies.lp_agile import managed_position as MP

    position_id = trigger.get("position_id") or ""
    pos = _lookup_position_state(position_id)
    if not pos:
        return {
            "trigger_key": trigger["_trigger_key"],
            "status": "POSITION_NOT_FOUND",
            "reason": f"could not find live state for {position_id}",
        }
    chain = (pos.get("chain") or "").lower()
    protocol = (pos.get("protocol") or "").lower()
    fee_tier = int(pos.get("fee_tier") or pos.get("fee") or 3000)
    pool_addr = (pos.get("pool_address") or "").lower()
    nft_token_id = int(pos.get("nft_token_id") or pos.get("token_id") or 0) or None
    new_lower, new_upper = _propose_new_ticks(pos)
    if new_lower is None:
        return {
            "trigger_key": trigger["_trigger_key"],
            "status": "TICK_PROPOSAL_FAILED",
            "reason": "could not derive new ticks from position state",
        }

    # Find or create the managed position
    mp = MP.find_by_pool(chain=chain, pool_address=pool_addr, fee_tier=fee_tier)
    if mp is None:
        mp_id = MP.upsert_managed_position(
            chain=chain, protocol=protocol,
            pool_address=pool_addr, fee_tier=fee_tier,
            token0_address=(pos.get("token0_address") or "").lower() or "0x0",
            token1_address=(pos.get("token1_address") or "").lower() or "0x0",
            token0_symbol=pos.get("token0_symbol"),
            token1_symbol=pos.get("token1_symbol"),
            current_nft_token_id=nft_token_id,
            current_tick_lower=int(pos.get("tick_lower") or 0),
            current_tick_upper=int(pos.get("tick_upper") or 0),
        )
    else:
        mp_id = int(mp["id"])

    # Build the calldata plan
    if chain in ("hyperevm", "hl-evm") and protocol == "prjx":
        try:
            from engine.strategies.lp_agile import prjx_adapter as PRJX
            plan = PRJX.rebalance_in_place(
                token_id=nft_token_id, new_tick_lower=new_lower,
                new_tick_upper=new_upper, fee_tier=fee_tier, dry_run=True,
            )
        except Exception as exc:                                    # noqa: BLE001
            return {
                "trigger_key": trigger["_trigger_key"],
                "managed_position_id": mp_id,
                "status": "PRJX_BUILD_FAILED",
                "error": f"{type(exc).__name__}: {exc}",
            }
    else:
        # Aerodrome / Slipstream / other: not yet implemented
        return {
            "trigger_key": trigger["_trigger_key"],
            "managed_position_id": mp_id,
            "status": "ADAPTER_NOT_BUILT",
            "reason": f"no in-place adapter for {chain}/{protocol} yet (task #162)",
            "proposed_ticks": [new_lower, new_upper],
        }

    # Record the planned mutation (no tx_hash yet — gates on live-send)
    try:
        mut_id = MP.record_mutation(
            managed_position_id=mp_id, action="range_adjust",
            before_nft_token_id=nft_token_id, after_nft_token_id=None,
            before_tick_lower=int(pos.get("tick_lower") or 0),
            before_tick_upper=int(pos.get("tick_upper") or 0),
            after_tick_lower=new_lower, after_tick_upper=new_upper,
            tx_hash=None, gas_cost_usd=None,
            triggered_by=f"auto:{trigger.get('trigger', 'unknown').lower()}",
            notes=f"V1 plan built — calldata len={plan.get('calldata_len')}",
        )
    except Exception as exc:                                        # noqa: BLE001
        mut_id = None
        logger.warning("[rebal_exec] mutation persist failed: %s", exc)

    return {
        "trigger_key": trigger["_trigger_key"],
        "managed_position_id": mp_id,
        "mutation_id": mut_id,
        "status": "PLAN_BUILT_LIVE_SEND_BLOCKED",
        "chain": chain, "protocol": protocol,
        "pool_address": pool_addr, "fee_tier": fee_tier,
        "old_nft_token_id": nft_token_id,
        "proposed_ticks": [new_lower, new_upper],
        "calldata_len": plan.get("calldata_len"),
        "est_gas_usd": plan.get("est_gas_cost_usd"),
        "deadline_ts": plan.get("deadline_ts"),
    }


def run_once() -> dict:
    """Drain the trigger queue once. Each pending trigger → plan → mutation
    log entry. Returns summary."""
    pending = _read_pending_triggers()
    out = {
        "n_pending": len(pending),
        "n_plans_built": 0,
        "n_skipped": 0,
        "plans": [],
    }
    for t in pending:
        plan = _build_plan_for_trigger(t)
        out["plans"].append(plan)
        status = plan.get("status", "?")
        outcome = "PLAN_BUILT" if status.startswith("PLAN_BUILT") else "SKIPPED"
        _mark_processed(t["_trigger_key"], action="build_plan",
                        outcome=outcome, detail=plan)
        if outcome == "PLAN_BUILT":
            out["n_plans_built"] += 1
        else:
            out["n_skipped"] += 1
    return out


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="rebalance_executor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run-once", help="Drain the trigger queue once")
    args = ap.parse_args()
    if args.cmd == "run-once":
        s = run_once()
        print(json.dumps(s, indent=2, default=str))
