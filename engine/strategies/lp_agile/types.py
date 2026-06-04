"""engine/strategies/lp_agile/types.py — shared dataclasses for the LP pillar.

Three layers:

  PoolSnapshot   — what a data adapter (PRJX / Uniswap V3 / Aerodrome) returns
                   for a single pool at a moment in time. Source-tagged + aged
                   so the trustless aggregator can verify before acting.

  RankedPool     — PoolSnapshot + the composite score from ranker.py. The unit
                   the scanner sorts by when deciding what to alert on.

  LPSignal       — actionable output the scanner emits. One of:
                       OPEN  : start a new LP position in a pool
                       CLOSE : exit a subscriber's existing position
                       REBAL : adjust range bounds without closing
                       HOLD  : explicit DO-NOTHING (drives "no action today")

  LPPosition     — tracked subscriber-side state when we know they opened on
                   our last OPEN alert. Phase 1 = best-effort (subscribers may
                   silently ignore us); Phase 2 = on-chain truth via the
                   dedicated LP wallet.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Protocol(str, Enum):
    """Which AMM protocol the pool lives on."""
    PRJX = "prjx"                # HyperEVM, V3-like concentrated liquidity
    UNISWAP_V3 = "uniswap_v3"    # Ethereum mainnet
    AERODROME = "aerodrome"      # Base — Solidly fork (v2 vAMM/sAMM)
    SLIPSTREAM = "slipstream"    # Base — Aerodrome's V3-like concentrated tier


class Chain(str, Enum):
    HYPEREVM = "hyperevm"
    ETHEREUM = "ethereum"
    BASE = "base"


class AssetClass(str, Enum):
    """For ranking + IL sizing. Stable-stable pairs IL-free; native/major both
    volatile."""
    NATIVE_STABLE = "native_stable"     # e.g. HYPE/USDC, ETH/USDC
    MAJOR_MAJOR = "major_major"          # e.g. WBTC/ETH
    STABLE_STABLE = "stable_stable"      # e.g. USDC/USDT
    LONGTAIL_STABLE = "longtail_stable"  # e.g. small-cap / USDC


class LPAction(str, Enum):
    OPEN = "open"
    CLOSE = "close"
    REBALANCE = "rebalance"
    HOLD = "hold"


# ---------------------------------------------------------------------------
# Source tagging — every read carries provenance + freshness
# (mirrors the trustless verification policy)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DataSource:
    """Where a PoolSnapshot's numbers came from.

    Adapters MUST populate this honestly. The ranker WILL refuse to score a
    pool whose snapshot is older than its protocol's max_age_s.
    """
    provider: str           # "prjx_rpc", "uniswap_v3_subgraph", "aerodrome_api"
    fetched_at: datetime    # UTC
    is_live: bool           # True = fresh fetch; False = cached/stale

    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.fetched_at).total_seconds()


# ---------------------------------------------------------------------------
# Pool catalogue entry (the universe.yaml row, parsed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolDef:
    """Static metadata about a pool — loaded from lp_universe.yaml."""
    id: str                          # canonical id, e.g. "prjx_hype_usdc"
    protocol: Protocol
    chain: Chain
    pair: str                        # display, e.g. "HYPE/USDC"
    base_symbol: str                 # volatile asset, e.g. "HYPE"
    quote_symbol: str                # quote asset, e.g. "USDC"
    pool_address: str                # 0x... — pool contract on the chain
    fee_tier_bps: int                # 5, 30, 100 — basis points
    asset_class: AssetClass
    tvl_usd_min: Decimal             # gating: don't recommend if below
    volume_usd_min_daily: Decimal    # gating
    audit_status: str                # "zellic_dec_2024", "audited_6mo_clean", etc.
    airdrop_eligibility: bool        # PRJX = True; mature AMMs = False
    notes: str = ""
    enabled: bool = True             # operator kill-switch
    # Slipstream uses tickSpacing as the factory key, not fee tier. When non-zero
    # the adapter prefers this over fee_tier_bps for pool resolution.
    tick_spacing: int = 0


# ---------------------------------------------------------------------------
# Pool snapshot — what an adapter returns per fetch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolSnapshot:
    """One pool's state at a moment in time."""
    pool: PoolDef
    fetched_at: datetime             # UTC
    source: DataSource

    # Price state
    base_price_usd: Decimal          # current price of the volatile asset
    quote_price_usd: Decimal         # ~1.00 for stable quotes; ETH/USDC has !=1 quote
    tick_current: Optional[int]      # V3-style current tick (None for v2 pools)

    # Liquidity / depth
    tvl_usd: Decimal                 # total value locked

    # Volume / fees
    volume_24h_usd: Decimal
    volume_7d_usd: Decimal
    fees_7d_usd: Decimal             # protocol fees collected by LPs over last 7d

    # Volatility (for range optimization)
    volatility_7d_sigma: Decimal     # 7-day stdev of base/quote price
    atr_7d_pct: Decimal              # ATR as % of price (intra-day swing proxy)

    # Airdrop / points (PRJX-style)
    airdrop_points_per_usd_day: Optional[Decimal] = None  # speculative
    speculative_airdrop_apr_est: Optional[Decimal] = None  # operator estimate

    # Derived (filled by ranker)
    fee_apr: Optional[Decimal] = None  # (fees_7d / tvl) × (365/7)

    # APR by source — populated by adapters that have multi-stream yield.
    # e.g. Aerodrome Slipstream: {"trading_fees": 470.0, "aero_emissions": 492.0}.
    # PRJX / Uniswap V3: usually just {"trading_fees": ...}.
    # total_apr() = sum of all values. Surfaced in PWA + BMI alerts + ranker.
    # Standing directive 2026-05-24 (Yomi): "we need to capture this and
    # for any other platform, a breakdown of APR — may not be limited to
    # the trading pair."
    apr_breakdown: dict = field(default_factory=dict)

    def total_apr(self) -> Decimal:
        """Sum of all APR sources. Falls back to fee_apr if breakdown empty."""
        if self.apr_breakdown:
            return Decimal(sum(Decimal(str(v)) for v in self.apr_breakdown.values()))
        return self.fee_apr or Decimal(0)

    # Token-side metadata for downstream consumers (range_optimizer, executor)
    # so they can compute ticks correctly without hard-coding decimals or
    # re-querying RPC. 2026-05-24: shipped per [[feedback-lp-tick-math-decimals]].
    token0_decimals: int = 18                # default = ETH-style
    token1_decimals: int = 6                 # default = USDC-style
    is_stable_0: bool = False                # token0 is a USD-pegged stable
    is_stable_1: bool = True                 # token1 is a USD-pegged stable


# ---------------------------------------------------------------------------
# Ranking output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RankedPool:
    """A pool with composite score attached."""
    snapshot: PoolSnapshot
    score: Decimal                   # 0.0 - 1.0 (higher = better)
    fee_apr_component: Decimal
    airdrop_component: Decimal
    il_risk_penalty: Decimal
    tvl_depth_component: Decimal
    volume_consistency_component: Decimal
    rationale: str                   # one-line explanation


# ---------------------------------------------------------------------------
# Signal (the alert payload)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LPSignal:
    """An actionable LP alert.

    Subscriber executes themselves (Phase 1) or LP auto-executor consumes
    (Phase 2 — wallet-segregated).
    """
    # Identity
    strategy_id: str                 # "lp_agile_subscriber_v1"
    generated_at_iso: str            # ISO UTC
    signal_id: str                   # short slug, e.g. "lp-prjx-hypeu-20260523-0734"
    action: LPAction

    # Target pool
    pool: PoolDef
    snapshot_at_signal: PoolSnapshot  # frozen at decision time

    # Range bounds (concentrated-liquidity inputs)
    range_low_price: Decimal          # base/quote price
    range_high_price: Decimal
    range_label: str                  # e.g. "balanced ±2σ", "aggressive ±1σ"

    # Sizing recommendation (always framed as suggestion, never instruction)
    suggested_capital_pct_of_lp_bankroll: Decimal  # e.g. Decimal("0.15") = 15%

    # Economics (projected at signal time)
    expected_daily_fee_usd_per_1k: Decimal     # USD/day per $1K position
    expected_airdrop_pts_per_day: Optional[Decimal]
    il_projection: dict                         # {"-20%": "-3.4%", "+20%": "-2.1%", ...}

    # Rationale + ranking context
    rationale: str                    # 2-3 sentences why this NOW
    ai_judge_verdict: str             # "PASS" | "WATCH" | "BLOCKED"
    ai_judge_tier: str                # "tier1_rule" | "tier2_gemini" | "tier3_claude"
    ai_judge_reasoning: str           # short summary from the AI judge

    # Comparison context (CLOSE / REBAL only)
    referenced_position_id: Optional[str] = None  # subscriber LPPosition id
    reason_code: Optional[str] = None             # e.g. "range_break_imminent"
    alternative_pool_id: Optional[str] = None     # better pool we're rotating to

    # For DO-NOTHING / HOLD
    hold_notes: str = ""

    # Metadata bucket for strategy-specific extras
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Position tracking (subscriber-side, best-effort Phase 1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LPPosition:
    """A tracked LP position.

    Phase 1: opened in response to one of our OPEN alerts; we record what we
    recommended even though we don't see the wallet directly. Future CLOSE /
    REBAL alerts reference this id.

    Phase 2: backed by on-chain LP NFT ownership in the dedicated LP wallet.
    """
    position_id: str                 # uuid-ish
    subscriber_id: str               # opaque to engine; correlates with BMI bot user
    pool: PoolDef
    opened_at: datetime
    opened_via_signal_id: str        # which alert started it
    range_low_price: Decimal
    range_high_price: Decimal
    suggested_capital_usd: Decimal   # we suggested this much
    # Phase 2 only — on-chain truth
    lp_nft_token_id: Optional[str] = None
    lp_wallet_address: Optional[str] = None
    last_verified_at: Optional[datetime] = None
    last_verified_value_usd: Optional[Decimal] = None
    closed_at: Optional[datetime] = None
    closed_via_signal_id: Optional[str] = None
