#!/usr/bin/env python3
"""engine/allocator/lp_tiered.py — LP Phase 2 tiered allocator (Phase 2a: suggestion-only).

Reads lp_tiers.yaml + current LP wallet state + ranked pool snapshots.
Computes what SHOULD be deployed per tier and emits SUGGESTIONS to a JSON
file the operator reviews. Does NOT execute mints, bridges, or swaps in
Phase 2a — Yomi approves each suggestion via PWA/Telegram (Phase 2b adds
auto-execute behind LP_AUTO_EXECUTE flag).

Locked spec: project_lp_phase_2_spec_2026_05_25 memory.

Suggestion shapes the allocator emits:
  - MINT     — open new LP position in a tier's pool
  - REBALANCE — close+reopen because price drifted out of range / pool dropped APR
  - BRIDGE    — move USDC between Base ↔ HyperEVM to satisfy tier weights
  - CLOSE     — exit a position that no longer meets tier's APR floor
  - HOLD      — no action (default)

Each suggestion includes: tier, pool, action, est_amount_usd, rationale,
gas_estimate, expected_apr, est_il_pct, idem_key (for de-dup on approval).

Output: ops/pwa/serve/lp_tier_suggestions.json (consumed by Premium portal
in Week 4; CLI for Yomi today).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("engine.allocator.lp_tiered")

_REPO_ROOT = Path(__file__).resolve().parents[2]
TIERS_YAML = _REPO_ROOT / "engine" / "strategies" / "lp_tiers.yaml"
LP_AGILE_LATEST = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_agile_latest.json"
SUGGESTIONS_OUT = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_tier_suggestions.json"


# ---------------------------------------------------------------------------
# Tier model
# ---------------------------------------------------------------------------

@dataclass
class TierConfig:
    strategy_id: str
    display_name: str
    pillar: str
    risk_class: str
    target_pct_of_lp_wallet: float
    target_apr_floor_pct: float
    rebalance_drift_pct: float
    pool_ids: list[str]
    notes: str


@dataclass
class Suggestion:
    suggestion_id: str
    tier_strategy_id: str
    action: str                # MINT / REBALANCE / BRIDGE / CLOSE / HOLD
    pool_id: Optional[str]
    chain: Optional[str]
    est_amount_usd: float
    rationale: str
    expected_apr_pct: Optional[float] = None
    est_il_pct: Optional[float] = None
    gas_estimate_usd: Optional[float] = None
    extra: dict = field(default_factory=dict)


def _load_tiers() -> tuple[dict, list[TierConfig]]:
    """Returns (top_level_config, list of tiers)."""
    if not TIERS_YAML.exists():
        raise FileNotFoundError(f"lp_tiers.yaml missing at {TIERS_YAML}")
    raw = yaml.safe_load(TIERS_YAML.read_text())
    top = {
        "total_lp_bankroll_usd": float(raw.get("total_lp_bankroll_usd", 100)),
        "per_position_max_usd": float(raw.get("per_position_max_usd", 25)),
        "lp_cb_drawdown_pct_threshold": float(raw.get("lp_cb_drawdown_pct_threshold", 5.0)),
        # SAFE-2.4 (2026-05-30): cross-pool correlation cap. Tiers are sized
        # independently, so HYPE can stack across HYPE/USDC + HYPE/BTC + HYPE/ETH
        # and a single HYPE move hits all of them at once. Cap aggregate exposure
        # to any one base asset at this % of the LP bankroll. yaml-overridable.
        "max_base_asset_pct_of_lp_wallet": float(raw.get("max_base_asset_pct_of_lp_wallet", 40.0)),
    }
    tiers = []
    for t in raw.get("tiers", []):
        tiers.append(TierConfig(
            strategy_id=t["strategy_id"],
            display_name=t.get("display_name", t["strategy_id"]),
            pillar=t.get("pillar", "lp"),
            risk_class=t.get("risk_class", "unknown"),
            target_pct_of_lp_wallet=float(t.get("target_pct_of_lp_wallet", 0)),
            target_apr_floor_pct=float(t.get("target_apr_floor_pct", 0)),
            rebalance_drift_pct=float(t.get("rebalance_drift_pct", 10)),
            pool_ids=list(t.get("pool_ids") or []),
            notes=t.get("notes", ""),
        ))
    return top, tiers


def _load_lp_agile_snapshot() -> Optional[dict]:
    """Loads the LP scanner output. Returns None if missing / stale."""
    if not LP_AGILE_LATEST.exists():
        logger.warning(f"[lp_tiered] lp_agile snapshot missing at {LP_AGILE_LATEST}")
        return None
    try:
        snap = json.loads(LP_AGILE_LATEST.read_text())
    except Exception as exc:
        logger.warning(f"[lp_tiered] snapshot parse failed: {exc}")
        return None
    # Staleness check — LP scanner runs every 30 min, refuse if >2h old
    try:
        gen = snap.get("generated_at_iso")
        if gen:
            gen_dt = datetime.fromisoformat(gen.replace("Z", "+00:00"))
            age_sec = (datetime.now(timezone.utc) - gen_dt).total_seconds()
            if age_sec > 7200:
                logger.warning(
                    f"[lp_tiered] lp_agile snapshot stale ({age_sec/60:.0f} min) — "
                    f"refusing to act on dead data"
                )
                return None
    except Exception:
        pass
    return snap


def _pools_by_id(snap: Optional[dict]) -> dict:
    """Index ranked pools by id from the LP snapshot."""
    if not snap:
        return {}
    return {p["id"]: p for p in snap.get("ranked_pools") or []}


def _open_positions(snap: Optional[dict]) -> list[dict]:
    """LP NFT positions currently held in the wallet, from the snapshot."""
    if not snap:
        return []
    return snap.get("open_positions") or []


# ---------------------------------------------------------------------------
# Dynamic bankroll (LP-REVAMP P0, 2026-06-01)
# ---------------------------------------------------------------------------
# The allocator sizes every tier as a PERCENT of bankroll, so the only thing
# that needs to scale with the wallet is the bankroll itself. Instead of a
# hardcoded total_lp_bankroll_usd, resolve it LIVE each run from the actual LP
# capital — so it self-sizes whether $1.5k lands tomorrow or it grows to $15k,
# with zero re-config. Priority: explicit env pin → live NAV → yaml fallback.

_WALLET_BALANCE_FILE = (_REPO_ROOT / "ops" / "opportunities" / "lp_wallet_balance.json")


def _idle_wallet_usd() -> float:
    """Un-deployed LP wallet USD across chains (mirrors lp_daily_pnl_watcher)."""
    try:
        if _WALLET_BALANCE_FILE.exists():
            d = json.loads(_WALLET_BALANCE_FILE.read_text())
            return float(d.get("total_usd_across_chains", 0) or 0)
    except Exception as exc:
        logger.warning(f"[lp_tiered] wallet balance read failed: {exc}")
    return 0.0


def _resolve_bankroll_usd(top_config: dict, snap: Optional[dict]) -> tuple[float, str]:
    """(bankroll_usd, source). Live NAV = idle wallet + deployed positions + fees."""
    env = os.environ.get("LP_BANKROLL_USD")
    if env:
        try:
            v = float(env)
            if v > 0:
                return v, "env_override"
        except ValueError:
            pass
    deployed = fees = 0.0
    for p in _open_positions(snap):
        try:
            deployed += float(p.get("value_usd") or 0)
            fees += float(p.get("fees_owed_usd") or 0)
        except Exception:
            pass
    nav = _idle_wallet_usd() + deployed + fees
    if nav > 0:
        return nav, "live_nav"
    return float(top_config.get("total_lp_bankroll_usd", 100)), "yaml_fallback"


# ---------------------------------------------------------------------------
# Suggestion generation per tier
# ---------------------------------------------------------------------------

def _suggest_for_tier(
    tier: TierConfig,
    top_config: dict,
    pools_by_id: dict,
    open_positions: list[dict],
    now_iso: str,
) -> list[Suggestion]:
    """Pure function: tier + market state → suggestions."""
    out: list[Suggestion] = []

    target_usd = top_config["total_lp_bankroll_usd"] * (tier.target_pct_of_lp_wallet / 100.0)
    per_position_max = top_config["per_position_max_usd"]

    if not tier.pool_ids:
        # Empty tier — log silently, no suggestion
        logger.debug(
            f"[lp_tiered] tier {tier.strategy_id} has no pool_ids — skipping"
        )
        return out

    # Map this tier's pools, filter to ones currently in the ranked snapshot
    tier_pool_data: list[dict] = []
    for pid in tier.pool_ids:
        p = pools_by_id.get(pid)
        if p is None:
            out.append(Suggestion(
                suggestion_id=f"{tier.strategy_id}:{pid}:missing:{now_iso[:13]}",
                tier_strategy_id=tier.strategy_id,
                action="HOLD",
                pool_id=pid, chain=None,
                est_amount_usd=0,
                rationale=f"Pool {pid} declared in tier but not present in latest LP scanner snapshot — investigate scanner.",
            ))
            continue
        tier_pool_data.append(p)

    if not tier_pool_data:
        return out

    # Filter to pools meeting tier's APR floor
    fundable_pools = [
        p for p in tier_pool_data
        if (p.get("fee_apr_pct") or 0) >= tier.target_apr_floor_pct
    ]
    if not fundable_pools:
        # All this tier's pools are below APR floor
        top_pool = tier_pool_data[0]
        out.append(Suggestion(
            suggestion_id=f"{tier.strategy_id}:no_apr:{now_iso[:13]}",
            tier_strategy_id=tier.strategy_id,
            action="HOLD",
            pool_id=top_pool["id"],
            chain=top_pool.get("chain"),
            est_amount_usd=0,
            rationale=(
                f"All tier pools below APR floor {tier.target_apr_floor_pct}%. "
                f"Top pool {top_pool['id']} at {top_pool.get('fee_apr_pct'):.1f}%."
            ),
            expected_apr_pct=top_pool.get("fee_apr_pct"),
        ))
        return out

    # Find current allocation in this tier
    tier_open_positions = [
        op for op in open_positions
        if any(
            (op.get("pool_address") or "").lower() ==
            (pools_by_id.get(pid, {}).get("pool_address") or "").lower()
            for pid in tier.pool_ids
        )
    ]
    current_usd = sum(
        float(op.get("value_usd") or 0) for op in tier_open_positions
    )

    drift_pct = abs(current_usd - target_usd) / target_usd * 100 if target_usd > 0 else 0

    # Decision tree.
    # Min viable mint = max(per_position_max / 5, $5). Avoids tiny dust mints
    # where gas eats >30% of position. If tier target is below this, suggest
    # a HOLD with explanation rather than a doomed mint.
    min_viable_mint_usd = max(per_position_max / 5.0, 5.0)

    if current_usd == 0 and target_usd >= min_viable_mint_usd:
        # Underdeployed — suggest a MINT (size capped at per_position_max,
        # smaller of: tier target OR per-position cap).
        top_pool = max(fundable_pools, key=lambda p: p.get("fee_apr_pct") or 0)
        mint_size = min(target_usd, per_position_max)
        out.append(Suggestion(
            suggestion_id=f"{tier.strategy_id}:mint:{top_pool['id']}:{now_iso[:13]}",
            tier_strategy_id=tier.strategy_id,
            action="MINT",
            pool_id=top_pool["id"],
            chain=top_pool.get("chain"),
            est_amount_usd=mint_size,
            rationale=(
                f"Tier currently empty. Target ${target_usd:.0f} of "
                f"${top_config['total_lp_bankroll_usd']:.0f} bankroll "
                f"(per-position cap ${per_position_max:.0f}). "
                f"Top fundable pool: {top_pool['id']} at "
                f"{top_pool.get('fee_apr_pct'):.1f}% APR."
            ),
            expected_apr_pct=top_pool.get("fee_apr_pct"),
            extra={"target_usd": target_usd, "per_position_max": per_position_max},
        ))
    elif current_usd == 0 and target_usd > 0:
        # Target is non-zero but below min viable mint — explain why we hold
        out.append(Suggestion(
            suggestion_id=f"{tier.strategy_id}:below_min_mint:{now_iso[:13]}",
            tier_strategy_id=tier.strategy_id,
            action="HOLD",
            pool_id=None, chain=None,
            est_amount_usd=0,
            rationale=(
                f"Tier target ${target_usd:.0f} is below minimum viable mint "
                f"${min_viable_mint_usd:.0f} (gas-as-%-of-position would be too high). "
                f"Scale bankroll OR raise tier target_pct_of_lp_wallet."
            ),
        ))
    elif drift_pct > tier.rebalance_drift_pct:
        # Drift triggers REBALANCE suggestion
        out.append(Suggestion(
            suggestion_id=f"{tier.strategy_id}:rebalance:{now_iso[:13]}",
            tier_strategy_id=tier.strategy_id,
            action="REBALANCE",
            pool_id=None, chain=None,
            est_amount_usd=current_usd - target_usd,  # signed: + = over, - = under
            rationale=(
                f"Tier drift {drift_pct:.1f}% > {tier.rebalance_drift_pct}% threshold. "
                f"Current ${current_usd:.0f} vs target ${target_usd:.0f}."
            ),
            extra={"open_positions_in_tier": len(tier_open_positions)},
        ))
    else:
        # In range — HOLD (the default)
        out.append(Suggestion(
            suggestion_id=f"{tier.strategy_id}:hold:{now_iso[:13]}",
            tier_strategy_id=tier.strategy_id,
            action="HOLD",
            pool_id=None, chain=None,
            est_amount_usd=0,
            rationale=(
                f"In range — current ${current_usd:.0f} vs target "
                f"${target_usd:.0f} (drift {drift_pct:.1f}%)."
            ),
        ))

    # OPPORTUNISTIC TOP_UP — task #100 per Yomi: "increase capital to highest
    # yield". When a fundable pool's APR materially exceeds the tier's
    # advertised target floor AND the tier is currently UNDER its hard
    # ceiling (target × 1.5 — the per-tier risk budget), suggest a top-up
    # beyond the static target. Still capped by per_position_max so we don't
    # blow risk discipline.
    #
    # Trigger: top_pool_apr > target_apr_floor × OPPORTUNITY_OUTPERFORM_MULT
    # AND   current_usd < target_usd × TIER_HARD_CEILING_MULT
    OPPORTUNITY_OUTPERFORM_MULT = 1.5    # APR must be 1.5× floor to trigger
    TIER_HARD_CEILING_MULT = 1.75        # can't exceed 175% of static target
    if fundable_pools:
        best = max(fundable_pools, key=lambda p: p.get("fee_apr_pct") or 0)
        best_apr = best.get("fee_apr_pct") or 0
        outperform = (
            tier.target_apr_floor_pct > 0
            and best_apr > tier.target_apr_floor_pct * OPPORTUNITY_OUTPERFORM_MULT
        )
        hard_ceiling = target_usd * TIER_HARD_CEILING_MULT
        headroom = hard_ceiling - current_usd
        if outperform and headroom >= min_viable_mint_usd:
            topup = min(headroom, per_position_max)
            out.append(Suggestion(
                suggestion_id=f"{tier.strategy_id}:topup:{best['id']}:{now_iso[:13]}",
                tier_strategy_id=tier.strategy_id,
                action="TOP_UP",
                pool_id=best["id"],
                chain=best.get("chain"),
                est_amount_usd=topup,
                rationale=(
                    f"OPPORTUNISTIC: {best['id']} at {best_apr:.0f}% APR is "
                    f"{best_apr / tier.target_apr_floor_pct:.1f}× the {tier.target_apr_floor_pct:.0f}% "
                    f"tier floor. Tier hard-ceiling ${hard_ceiling:.0f} (1.75× target ${target_usd:.0f}); "
                    f"current ${current_usd:.0f}. Top up ${topup:.0f} to capture excess yield while "
                    f"staying inside risk budget."
                ),
                expected_apr_pct=best_apr,
                extra={
                    "tier_static_target_usd": target_usd,
                    "tier_hard_ceiling_usd": hard_ceiling,
                    "per_position_max_usd": per_position_max,
                    "outperform_mult": round(best_apr / max(tier.target_apr_floor_pct, 1), 2),
                },
            ))

    return out


# ---------------------------------------------------------------------------
# SAFE-2.4 — cross-pool correlation cap (per base asset)
# ---------------------------------------------------------------------------

def _base_asset(pool: Optional[dict]) -> Optional[str]:
    """The primary (volatile) asset of a pool — the first token of its pair.
    'HYPE/USDC' → 'HYPE', 'HYPE/BTC' → 'HYPE', 'cbBTC/USDC' → 'CBBTC'.
    So all HYPE-paired pools share one exposure bucket."""
    if not pool:
        return None
    pair = pool.get("pair") or pool.get("symbol")
    if pair and "/" in str(pair):
        return str(pair).split("/")[0].strip().upper() or None
    sym = pool.get("token0_symbol") or pool.get("base_symbol")
    return str(sym).strip().upper() if sym else None


def _apply_base_asset_caps(
    suggestions: list[Suggestion], top_config: dict,
    pools_by_id: dict, open_positions: list[dict],
) -> dict:
    """Trim / hold new MINT & TOP_UP suggestions so aggregate exposure to any
    single base asset stays under max_base_asset_pct_of_lp_wallet of bankroll.
    Mutates `suggestions` in place. Returns a per-base exposure summary."""
    bankroll = top_config["total_lp_bankroll_usd"]
    cap_usd = bankroll * (top_config["max_base_asset_pct_of_lp_wallet"] / 100.0)
    per_position_max = top_config["per_position_max_usd"]
    min_viable = max(per_position_max / 5.0, 5.0)

    # address → base, for valuing existing positions
    addr_to_base = {}
    for p in pools_by_id.values():
        addr = (p.get("pool_address") or "").lower()
        if addr:
            addr_to_base[addr] = _base_asset(p)

    # current committed exposure per base from open positions
    committed: dict[str, float] = {}
    for op in open_positions:
        base = addr_to_base.get((op.get("pool_address") or "").lower())
        if base:
            committed[base] = committed.get(base, 0.0) + float(op.get("value_usd") or 0)

    for s in suggestions:
        if s.action not in ("MINT", "TOP_UP"):
            continue
        base = _base_asset(pools_by_id.get(s.pool_id))
        if not base:
            continue
        cur = committed.get(base, 0.0)
        headroom = cap_usd - cur
        if headroom <= 0:
            s.extra["base_asset_cap"] = {
                "base": base, "cap_usd": round(cap_usd, 2),
                "already_committed_usd": round(cur, 2), "action": "blocked",
            }
            s.rationale = (
                f"[CORRELATION CAP] {base} exposure ${cur:.0f} already at/over the "
                f"${cap_usd:.0f} cap ({top_config['max_base_asset_pct_of_lp_wallet']:.0f}% "
                f"of bankroll). Holding instead of: {s.rationale}"
            )
            s.action = "HOLD"
            s.est_amount_usd = 0.0
        elif s.est_amount_usd > headroom:
            if headroom < min_viable:
                s.extra["base_asset_cap"] = {
                    "base": base, "cap_usd": round(cap_usd, 2),
                    "already_committed_usd": round(cur, 2),
                    "headroom_usd": round(headroom, 2), "action": "blocked_tiny_headroom",
                }
                s.rationale = (
                    f"[CORRELATION CAP] {base} headroom ${headroom:.0f} below min viable "
                    f"mint ${min_viable:.0f} (cap ${cap_usd:.0f}). Holding instead of: {s.rationale}"
                )
                s.action = "HOLD"
                s.est_amount_usd = 0.0
            else:
                orig = s.est_amount_usd
                s.est_amount_usd = round(headroom, 2)
                s.extra["base_asset_cap"] = {
                    "base": base, "cap_usd": round(cap_usd, 2),
                    "trimmed_from_usd": round(orig, 2), "trimmed_to_usd": round(headroom, 2),
                    "action": "trimmed",
                }
                s.rationale = (
                    f"[CORRELATION CAP] trimmed ${orig:.0f}→${headroom:.0f} to keep {base} "
                    f"aggregate under ${cap_usd:.0f}. {s.rationale}"
                )
                committed[base] = cur + headroom
        else:
            committed[base] = cur + s.est_amount_usd

    return {
        "cap_usd": round(cap_usd, 2),
        "cap_pct_of_bankroll": top_config["max_base_asset_pct_of_lp_wallet"],
        "committed_by_base_usd": {k: round(v, 2) for k, v in sorted(committed.items())},
    }


# ---------------------------------------------------------------------------
# Top-level run
# ---------------------------------------------------------------------------

def run_suggest(*, write_file: bool = True) -> dict:
    """Compute suggestions across all tiers. Returns full report dict.

    Phase 2a: SUGGESTIONS only. Does NOT execute anything. The operator
    reviews and approves via Telegram or CLI.
    """
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    try:
        top_config, tiers = _load_tiers()
    except FileNotFoundError as exc:
        return {"error": str(exc), "ts_iso": now_iso}

    snap = _load_lp_agile_snapshot()
    pools_by_id = _pools_by_id(snap)
    open_positions = _open_positions(snap)

    # LP-REVAMP P0: bankroll self-sizes off live LP NAV (idle wallet + deployed
    # + fees). Tiers are % of this, so capital growth needs no re-config.
    bankroll, bankroll_source = _resolve_bankroll_usd(top_config, snap)
    top_config["total_lp_bankroll_usd"] = round(bankroll, 2)
    top_config["bankroll_source"] = bankroll_source

    all_suggestions: list[Suggestion] = []
    per_tier_status = []
    for tier in tiers:
        tier_suggestions = _suggest_for_tier(
            tier, top_config, pools_by_id, open_positions, now_iso,
        )
        all_suggestions.extend(tier_suggestions)
        per_tier_status.append({
            "strategy_id": tier.strategy_id,
            "display_name": tier.display_name,
            "target_pct_of_lp_wallet": tier.target_pct_of_lp_wallet,
            "n_pools_configured": len(tier.pool_ids),
            "n_pools_in_snapshot": sum(1 for pid in tier.pool_ids if pid in pools_by_id),
            "n_suggestions": len(tier_suggestions),
        })

    # SAFE-2.4: enforce the per-base-asset correlation cap ACROSS tiers, after
    # all independent tier suggestions are in. Trims/holds over-concentrated mints.
    base_asset_exposure = _apply_base_asset_caps(
        all_suggestions, top_config, pools_by_id, open_positions,
    )

    report = {
        "generated_at_iso": now_iso,
        "config": top_config,
        "lp_agile_snapshot_age_sec": (
            (now - datetime.fromisoformat(snap["generated_at_iso"].replace("Z", "+00:00"))).total_seconds()
            if snap and snap.get("generated_at_iso") else None
        ),
        "lp_agile_snapshot_available": snap is not None,
        "open_positions_total": len(open_positions),
        "per_tier_status": per_tier_status,
        "base_asset_exposure": base_asset_exposure,
        "suggestions": [_suggestion_to_dict(s) for s in all_suggestions],
        "execution_mode": "SUGGESTION_ONLY",  # Phase 2a marker
        "spec_memory": "project_lp_phase_2_spec_2026_05_25",
    }

    if write_file:
        try:
            SUGGESTIONS_OUT.parent.mkdir(parents=True, exist_ok=True)
            tmp = SUGGESTIONS_OUT.with_suffix(".tmp")
            tmp.write_text(json.dumps(report, indent=2, default=str))
            tmp.replace(SUGGESTIONS_OUT)
            logger.info(f"[lp_tiered] wrote {SUGGESTIONS_OUT}")
        except Exception as exc:
            logger.error(f"[lp_tiered] suggestions write failed: {exc}")

    return report


def _suggestion_to_dict(s: Suggestion) -> dict:
    return {
        "suggestion_id": s.suggestion_id,
        "tier_strategy_id": s.tier_strategy_id,
        "action": s.action,
        "pool_id": s.pool_id,
        "chain": s.chain,
        "est_amount_usd": round(s.est_amount_usd, 2),
        "rationale": s.rationale,
        "expected_apr_pct": (
            round(s.expected_apr_pct, 2) if s.expected_apr_pct is not None else None
        ),
        "est_il_pct": s.est_il_pct,
        "gas_estimate_usd": s.gas_estimate_usd,
        "extra": s.extra,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="LP Phase 2 tiered allocator (suggestion-only)")
    p.add_argument("--no-write", action="store_true",
                   help="print suggestions without writing to disk")
    p.add_argument("--pretty", action="store_true",
                   help="human-readable summary instead of JSON")
    args = p.parse_args(argv)

    report = run_suggest(write_file=not args.no_write)

    if args.pretty:
        _print_pretty(report)
    else:
        print(json.dumps(report, indent=2, default=str))
    return 0


def _print_pretty(report: dict) -> None:
    print(f"\n{'='*72}")
    print(f"  LP TIERED ALLOCATOR — {report.get('generated_at_iso','')[:19]}")
    print(f"  Mode: {report.get('execution_mode')}  (Phase 2a — suggestions only)")
    print(f"{'='*72}")
    cfg = report.get("config") or {}
    print(f"  Bankroll: ${cfg.get('total_lp_bankroll_usd')} · "
          f"Per-position max: ${cfg.get('per_position_max_usd')} · "
          f"LP CB threshold: -{cfg.get('lp_cb_drawdown_pct_threshold')}%")
    print()
    print(f"  Tier status:")
    for t in report.get("per_tier_status") or []:
        print(f"    • {t['display_name']:<28}  "
              f"target {t['target_pct_of_lp_wallet']:>3.0f}%  "
              f"pools {t['n_pools_in_snapshot']}/{t['n_pools_configured']}  "
              f"→ {t['n_suggestions']} suggestion(s)")
    print()
    print(f"  Suggestions ({len(report.get('suggestions') or [])}):")
    for s in report.get("suggestions") or []:
        emoji = {
            "MINT": "🟢", "REBALANCE": "🟡", "BRIDGE": "🔵",
            "CLOSE": "🟠", "HOLD": "⚪",
        }.get(s.get("action"), "?")
        amt = f"${s.get('est_amount_usd', 0):.0f}" if s.get('est_amount_usd') else ""
        print(f"    {emoji} {s.get('action'):<10} {s.get('tier_strategy_id'):<25} "
              f"{s.get('pool_id') or '—':<35} {amt}")
        print(f"       {s.get('rationale')}")
    print()


if __name__ == "__main__":
    sys.exit(main())
