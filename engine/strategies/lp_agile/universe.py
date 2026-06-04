"""engine/strategies/lp_agile/universe.py — load + validate lp_universe.yaml.

Single entry point: load_pool_universe(). Reads the canonical yaml, parses
each entry into a PoolDef, runs inclusion-gate validation, and returns the
ENABLED subset.

Operator weights + freshness windows are also returned so the scanner can
configure itself from the same source-of-truth file.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional

import yaml

from engine.strategies.lp_agile.types import (
    AssetClass, Chain, PoolDef, Protocol,
)

logger = logging.getLogger("engine.strategies.lp_agile.universe")

DEFAULT_YAML_PATH = (
    Path(__file__).resolve().parent.parent / "lp_universe.yaml"
)


@dataclass(frozen=True)
class UniverseConfig:
    pools: list[PoolDef]                       # enabled-only
    all_pools: list[PoolDef]                   # including disabled (for ops/debug)
    weights: dict                              # default_weights from yaml
    freshness_max_s: dict                      # protocol -> int seconds
    yaml_version: int
    generated_at: str


def load_pool_universe(path: Optional[Path] = None) -> UniverseConfig:
    """Parse lp_universe.yaml. Raises if file missing or required fields absent.

    Disabled pools (`enabled: false`) are returned in `all_pools` only — the
    scanner consumes `.pools`.
    """
    path = path or DEFAULT_YAML_PATH
    if not path.exists():
        raise FileNotFoundError(f"lp_universe.yaml not found at {path}")

    raw = yaml.safe_load(path.read_text())

    weights = raw.get("default_weights") or {}
    _validate_weights(weights)

    freshness = raw.get("protocol_freshness_max_s") or {}
    if not freshness:
        raise ValueError("lp_universe.yaml missing protocol_freshness_max_s")

    raw_pools = raw.get("pools") or []
    if not raw_pools:
        raise ValueError("lp_universe.yaml has no pools")

    all_pools: list[PoolDef] = []
    for entry in raw_pools:
        pool = _parse_pool(entry)
        all_pools.append(pool)

    enabled = [p for p in all_pools if p.enabled]
    logger.info(
        "lp_universe loaded: %d enabled / %d total across protocols %s",
        len(enabled), len(all_pools),
        sorted({p.protocol.value for p in enabled}),
    )

    return UniverseConfig(
        pools=enabled,
        all_pools=all_pools,
        weights=weights,
        freshness_max_s=freshness,
        yaml_version=int(raw.get("version", 0)),
        generated_at=str(raw.get("generated_at", "")),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


REQUIRED_POOL_FIELDS = {
    "id", "protocol", "chain", "pair", "base_symbol", "quote_symbol",
    "pool_address", "fee_tier_bps", "asset_class",
    "tvl_usd_min", "volume_usd_min_daily", "audit_status",
    "airdrop_eligibility",
}


def _parse_pool(entry: dict) -> PoolDef:
    missing = REQUIRED_POOL_FIELDS - set(entry.keys())
    if missing:
        raise ValueError(
            f"pool {entry.get('id', '?')} missing required fields: {sorted(missing)}"
        )

    try:
        protocol = Protocol(entry["protocol"])
    except ValueError as e:
        raise ValueError(
            f"pool {entry['id']}: unknown protocol '{entry['protocol']}' "
            f"— must be one of {[p.value for p in Protocol]}"
        ) from e

    try:
        chain = Chain(entry["chain"])
    except ValueError as e:
        raise ValueError(
            f"pool {entry['id']}: unknown chain '{entry['chain']}'"
        ) from e

    try:
        asset_class = AssetClass(entry["asset_class"])
    except ValueError as e:
        raise ValueError(
            f"pool {entry['id']}: unknown asset_class '{entry['asset_class']}'"
        ) from e

    return PoolDef(
        id=entry["id"],
        protocol=protocol,
        chain=chain,
        pair=entry["pair"],
        base_symbol=entry["base_symbol"].upper(),
        quote_symbol=entry["quote_symbol"].upper(),
        pool_address=str(entry["pool_address"]),
        fee_tier_bps=int(entry["fee_tier_bps"]),
        asset_class=asset_class,
        tvl_usd_min=Decimal(str(entry["tvl_usd_min"])),
        volume_usd_min_daily=Decimal(str(entry["volume_usd_min_daily"])),
        audit_status=str(entry["audit_status"]),
        airdrop_eligibility=bool(entry["airdrop_eligibility"]),
        notes=str(entry.get("notes", "")),
        enabled=bool(entry.get("enabled", True)),
        tick_spacing=int(entry.get("tick_spacing", 0)),
    )


def _validate_weights(weights: dict) -> None:
    required = {"fee_apr", "airdrop_apr_est", "il_risk",
                "tvl_depth", "volume_consistency"}
    missing = required - set(weights.keys())
    if missing:
        raise ValueError(
            f"default_weights missing required keys: {sorted(missing)}"
        )
    # Tolerance: weights don't need to sum to 1.0 (penalty + bonus mix), but
    # nothing should be negative — that's caller intent confusion.
    for k, v in weights.items():
        if float(v) < 0:
            raise ValueError(f"default_weights['{k}'] is negative ({v}) — invalid")
