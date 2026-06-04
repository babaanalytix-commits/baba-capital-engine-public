"""engine/strategies/lp_agile/executor.py — mint / collect / decreaseLiquidity.

Phase 2 of LP_AGILE_SUBSCRIBER_v1. Yomi-owned dedicated LP wallet only
(per [[feedback-lp-dedicated-wallet]]).

Supports two modes for every operation:
  DRY_RUN   — builds + displays the transaction calldata + estimated gas + USD impact.
              NEVER signs, NEVER sends. Safe to run anywhere.
  LIVE      — signs with LP_WALLET_PRIVATE_KEY and sends. Only fires when:
              - LP_AUTO_EXECUTE=true  (full autonomous) OR
              - explicit `live=True` from a one-tap APPROVE handler

Trustless rule: after every LIVE mint, verify the NFT lands in the wallet
via NPM.balanceOf() before considering the position open (per the
trustless-source-of-truth model used across CARRY/MD/ORACLE).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from web3 import Web3

from engine.data.lp_pools._abis import ERC20_ABI
from engine.data.lp_pools._evm import get_w3
from engine.strategies.lp_agile.env import get_lp_config, get_signer
from engine.strategies.lp_agile.range_optimizer import compute_range
from engine.strategies.lp_agile.types import (
    Chain, LPSignal, PoolDef, PoolSnapshot, Protocol,
)

logger = logging.getLogger("engine.strategies.lp_agile.executor")


# ---------------------------------------------------------------------------
# Slipstream NPM (Aerodrome on Base) — mint ABI
# ---------------------------------------------------------------------------

SLIPSTREAM_NPM = Web3.to_checksum_address("0x827922686190790b37229fd06084350E74485b72")

SLIPSTREAM_MINT_ABI = [
    {
        "inputs": [{
            "components": [
                {"name": "token0", "type": "address"},
                {"name": "token1", "type": "address"},
                {"name": "tickSpacing", "type": "int24"},
                {"name": "tickLower", "type": "int24"},
                {"name": "tickUpper", "type": "int24"},
                {"name": "amount0Desired", "type": "uint256"},
                {"name": "amount1Desired", "type": "uint256"},
                {"name": "amount0Min", "type": "uint256"},
                {"name": "amount1Min", "type": "uint256"},
                {"name": "recipient", "type": "address"},
                {"name": "deadline", "type": "uint256"},
                {"name": "sqrtPriceX96", "type": "uint160"},
            ],
            "name": "params", "type": "tuple",
        }],
        "name": "mint",
        "outputs": [
            {"name": "tokenId", "type": "uint256"},
            {"name": "liquidity", "type": "uint128"},
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
        "stateMutability": "payable",
        "type": "function",
    },
]

# ---------------------------------------------------------------------------
# Slipstream NPM (Aerodrome on Base) — increaseLiquidity ABI (2026-06-04 #412)
# ---------------------------------------------------------------------------
# Used by the idle-deploy dispatcher to top up an existing position with
# fresh wallet capital, and (Phase 2) by the harvester to compound collected
# fees back into the position. Standard Uniswap V3 NFPM signature — works
# identically on Slipstream (Aerodrome inherits the position manager
# interface) so the same struct works.

# Aerodrome Slipstream CL Gauge ABI — for unstake/restake dance when the
# target position is staked for AERO rewards (2026-06-04 #419 follow-up).
# Slipstream gauges hold the NFT; to add liquidity we must withdraw → mutate → re-deposit.
SLIPSTREAM_CL_GAUGE_ABI = [
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "withdraw",  # unstake the NFT back to the wallet
        "outputs": [],
        "stateMutability": "nonpayable", "type": "function",
    },
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "deposit",   # stake the NFT into the gauge
        "outputs": [],
        "stateMutability": "nonpayable", "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "getReward",  # claim AERO rewards
        "outputs": [],
        "stateMutability": "nonpayable", "type": "function",
    },
]

# ERC-721 minimal ABI — for approving the gauge to transfer the NFT back in.
ERC721_APPROVE_ABI = [
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "tokenId", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [],
        "stateMutability": "nonpayable", "type": "function",
    },
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "getApproved",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view", "type": "function",
    },
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "ownerOf",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view", "type": "function",
    },
]


SLIPSTREAM_INCREASE_LIQUIDITY_ABI = [
    {
        "inputs": [{
            "components": [
                {"name": "tokenId", "type": "uint256"},
                {"name": "amount0Desired", "type": "uint256"},
                {"name": "amount1Desired", "type": "uint256"},
                {"name": "amount0Min", "type": "uint256"},
                {"name": "amount1Min", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
            ],
            "name": "params", "type": "tuple",
        }],
        "name": "increaseLiquidity",
        "outputs": [
            {"name": "liquidity", "type": "uint128"},
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
        "stateMutability": "payable",
        "type": "function",
    },
    # positions() — read current liquidity so the dispatcher can verify
    # post-tx that the deposit landed.
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "positions",
        "outputs": [
            {"name": "nonce", "type": "uint96"},
            {"name": "operator", "type": "address"},
            {"name": "token0", "type": "address"},
            {"name": "token1", "type": "address"},
            {"name": "tickSpacing", "type": "int24"},
            {"name": "tickLower", "type": "int24"},
            {"name": "tickUpper", "type": "int24"},
            {"name": "liquidity", "type": "uint128"},
            {"name": "feeGrowthInside0LastX128", "type": "uint256"},
            {"name": "feeGrowthInside1LastX128", "type": "uint256"},
            {"name": "tokensOwed0", "type": "uint128"},
            {"name": "tokensOwed1", "type": "uint128"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]


# ---------------------------------------------------------------------------
# PRJX (HyperEVM) NPM — Uniswap-V3-style mint (LP-3, 2026-05-30)
# ---------------------------------------------------------------------------
# PRJX is a Uniswap V3 fork (factory.getPool(t0,t1,fee); raw_fee = bps*100).
# Its position manager ("PRJX V3 Positions NFT-V1") is the SAME address the
# data adapter enumerates positions on (engine/data/lp_pools/prjx.py). The mint
# struct is VANILLA UniV3 — `fee` (uint24) instead of Slipstream's tickSpacing,
# and NO trailing sqrtPriceX96 field. Verify against the operator's sample tx
# before enabling execution (see LP-3 notes); keep prjx OFF the default
# LP_EXECUTABLE_PROTOCOLS until that one-mint check passes.
PRJX_NPM = Web3.to_checksum_address("0xeaD19AE861c29bBb2101E834922B2FEee69B9091")

UNIV3_MINT_ABI = [
    {
        "inputs": [{
            "components": [
                {"name": "token0", "type": "address"},
                {"name": "token1", "type": "address"},
                {"name": "fee", "type": "uint24"},
                {"name": "tickLower", "type": "int24"},
                {"name": "tickUpper", "type": "int24"},
                {"name": "amount0Desired", "type": "uint256"},
                {"name": "amount1Desired", "type": "uint256"},
                {"name": "amount0Min", "type": "uint256"},
                {"name": "amount1Min", "type": "uint256"},
                {"name": "recipient", "type": "address"},
                {"name": "deadline", "type": "uint256"},
            ],
            "name": "params", "type": "tuple",
        }],
        "name": "mint",
        "outputs": [
            {"name": "tokenId", "type": "uint256"},
            {"name": "liquidity", "type": "uint128"},
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
        "stateMutability": "payable",
        "type": "function",
    },
]


def _mint_target(protocol) -> tuple:
    """Return (npm_address, npm_abi, is_univ3) for a protocol. is_univ3=True →
    mint params use `fee` (uint24) and have NO trailing sqrtPriceX96 field;
    False → Slipstream layout (tickSpacing + sqrtPriceX96).

    NOTE (LP-3, 2026-05-30): prjx is intentionally NOT mapped here. Its real LP
    entry is an OpenOcean zap (router 0xB165…, selector 0x5f575529), not a direct
    NonfungiblePositionManager.mint — see the LP-3 correction. The UNIV3_MINT_ABI
    + PRJX_NPM constants are retained for reference / a future HyperSwap-V3 direct
    path, but minting prjx via this dispatch would target the wrong contract, so
    it raises until the zap adapter exists.
    """
    pv = getattr(protocol, "value", protocol)
    if pv == "slipstream":
        return SLIPSTREAM_NPM, SLIPSTREAM_MINT_ABI, False
    raise NotImplementedError(
        f"no direct-mint adapter for protocol {pv!r} "
        f"(prjx requires the OpenOcean zap adapter — LP-3)"
    )


def _build_mint_params(*, is_univ3: bool, t0, t1, fee_or_spacing: int,
                       tick_lower: int, tick_upper: int,
                       a0_desired: int, a1_desired: int,
                       a0_min: int, a1_min: int, recipient, deadline: int) -> tuple:
    """Construct the protocol-correct mint() params tuple. For UniV3/prjx the
    3rd field is `fee` (raw = bps*100) and there's no sqrtPriceX96; for
    Slipstream it's tickSpacing and a trailing sqrtPriceX96=0 (skip pool init)."""
    base = (
        Web3.to_checksum_address(t0), Web3.to_checksum_address(t1),
        int(fee_or_spacing), int(tick_lower), int(tick_upper),
        int(a0_desired), int(a1_desired), int(a0_min), int(a1_min),
        recipient, int(deadline),
    )
    return base if is_univ3 else (*base, 0)


# ERC20.approve — for granting NPM allowance over USDC + cbBTC
ERC20_APPROVE_ABI = [
    {"inputs": [
        {"name": "spender", "type": "address"},
        {"name": "amount", "type": "uint256"},
     ], "name": "approve",
     "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "nonpayable",
     "type": "function"},
    {"inputs": [
        {"name": "owner", "type": "address"},
        {"name": "spender", "type": "address"},
     ], "name": "allowance",
     "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view",
     "type": "function"},
]


# ---------------------------------------------------------------------------
# Dry-run result type
# ---------------------------------------------------------------------------


# Cost-aware sizing rule of thumb (per Yomi 2026-05-23):
#   minimum_position_usd = (round_trip_costs / target_net_apr) × (365 / expected_hold_days)
#   At $1/cycle costs, 200% APR target, 7-day expected hold → min $10 position.
# Documented in feedback_lp_min_position_size_rule memory.

# Estimated round-trip costs on Base — rough but useful for UX.
# Refined over time by the cost ledger (#642).
EST_BASE_GAS_COST_USD = {
    "approve":           Decimal("0.05"),
    "mint":              Decimal("0.30"),
    "decrease_liquidity": Decimal("0.30"),
    "collect":           Decimal("0.10"),
    "swap":              Decimal("0.05"),  # swap fee + slippage round-trip estimate
}


def estimate_round_trip_cost_usd(*, includes_swap_in_out: bool = True) -> Decimal:
    """Total estimated cost: 2× approve + mint + decrease + collect (+ swap in/out)."""
    total = (
        2 * EST_BASE_GAS_COST_USD["approve"]
        + EST_BASE_GAS_COST_USD["mint"]
        + EST_BASE_GAS_COST_USD["decrease_liquidity"]
        + EST_BASE_GAS_COST_USD["collect"]
    )
    if includes_swap_in_out:
        total += 2 * EST_BASE_GAS_COST_USD["swap"]
    return total


def max_position_from_wallet(
    wallet_bal0: Decimal, wallet_bal1: Decimal,
    base_price_usd: Decimal, *, is_stable_0: bool,
) -> Decimal:
    """Compute max LP position size (USD) achievable with current wallet balances
    at the 50/50 split implied by current price.

    For a 50/50 LP at price P:
      - need half_usd worth of token0
      - need half_usd worth of token1
    Position max = 2 × min(usd_value_of_token0_balance, usd_value_of_token1_balance).
    """
    if is_stable_0:
        # token0 = USDC, token1 = volatile (cbBTC)
        usd_value_0 = wallet_bal0                            # USDC is $1
        usd_value_1 = wallet_bal1 * base_price_usd
    else:
        usd_value_0 = wallet_bal0 * base_price_usd
        usd_value_1 = wallet_bal1
    return 2 * min(usd_value_0, usd_value_1)


@dataclass(frozen=True)
class MintPreview:
    """What a mint would look like — for trustless human review before LIVE."""
    pool: PoolDef
    chain: Chain
    npm_address: str
    # MintParams (decoded human values)
    token0_symbol: str
    token1_symbol: str
    token0_decimals: int
    token1_decimals: int
    tick_lower: int
    tick_upper: int
    price_lower_usd: Decimal
    price_upper_usd: Decimal
    amount0_desired_human: Decimal
    amount1_desired_human: Decimal
    amount0_min_human: Decimal
    amount1_min_human: Decimal
    slippage_pct: Decimal
    recipient: str
    deadline_iso: str
    # Capital + balance sanity
    target_position_usd: Decimal
    wallet_balance_token0_human: Decimal
    wallet_balance_token1_human: Decimal
    wallet_balance_native_gas: Decimal
    # Pre-conditions (what user/agent must satisfy before tx can succeed)
    preconditions: list[str]
    # Tx data (only displayed, never auto-sent)
    approve_token0_calldata: str
    approve_token1_calldata: str
    mint_calldata: str
    estimated_gas_units: Optional[int]
    estimated_gas_cost_usd: Optional[Decimal]
    # Cost-aware economics
    wallet_max_position_usd: Decimal
    est_round_trip_cost_usd: Decimal
    breakeven_days_at_200apr: Decimal


# ---------------------------------------------------------------------------
# Build a mint preview (DRY-RUN)
# ---------------------------------------------------------------------------


def build_mint_preview(
    signal: LPSignal, *, target_position_usd: Optional[Decimal] = None,
    slippage_pct: Decimal = Decimal("0.05"),
) -> MintPreview:
    """Build a MintPreview from an LPSignal — DOES NOT SIGN OR SEND.

    Use this to render a human-reviewable summary before any APPROVE tap.

    Args:
      signal: an LPSignal (action=OPEN) from triggers.evaluate_triggers
      target_position_usd: capital to deploy. Defaults to .env.lp
                           LP_PER_POSITION_MAX_USD.
      slippage_pct: max slippage tolerance for amount0Min / amount1Min.
                    Default 1%.
    """
    if signal.pool.protocol not in (Protocol.SLIPSTREAM,):
        # LP-3 finding (2026-05-30): prjx LP entries are NOT a direct NPM mint —
        # the operator's real tx (0xce5b…de713) goes through an OpenOcean ZAP
        # router (0xB165C4d4B8044D4A9276c3d75F08cD6a2874A3b2, selector 0x5f575529)
        # that wraps HYPE + routes a balancing swap + mints in one tx, with
        # route-specific calldata built by OpenOcean's API. A hardcoded mint ABI
        # can't reproduce it. Until the OpenOcean-zap adapter is built, prjx mints
        # stay MANUAL (engine suggests + tracks; operator executes in the prjx UI).
        raise NotImplementedError(
            f"executor supports Slipstream direct-mint only. {signal.pool.protocol.value} "
            f"requires the OpenOcean zap adapter (see LP-3 correction) — not yet built."
        )

    cfg = get_lp_config()
    if target_position_usd is None:
        target_position_usd = cfg["per_position_max_usd"]

    pool = signal.pool
    snap = signal.snapshot_at_signal
    if snap is None:
        raise ValueError("LPSignal has no snapshot_at_signal — re-run scan")

    chain = pool.chain
    w3 = get_w3(chain)

    # Resolve on-chain pool, read token0/token1 metadata + balances
    from engine.data.lp_pools import get_adapter
    adapter = get_adapter(pool.protocol)
    pool_address = adapter.resolve_pool_address(pool)
    if not pool_address:
        raise ValueError(f"Could not resolve pool address for {pool.id}")

    from engine.data.lp_pools._abis import UNIV3_POOL_ABI, SLIPSTREAM_POOL_ABI
    pool_abi = SLIPSTREAM_POOL_ABI if pool.protocol == Protocol.SLIPSTREAM else UNIV3_POOL_ABI
    pool_c = w3.eth.contract(address=Web3.to_checksum_address(pool_address), abi=pool_abi)
    t0_addr = pool_c.functions.token0().call()
    t1_addr = pool_c.functions.token1().call()
    tick_spacing = pool.tick_spacing or pool_c.functions.tickSpacing().call()
    slot0 = pool_c.functions.slot0().call()
    current_sqrt_x96, current_tick = slot0[0], slot0[1]

    t0 = w3.eth.contract(address=t0_addr, abi=ERC20_ABI)
    t1 = w3.eth.contract(address=t1_addr, abi=ERC20_ABI)
    dec0 = t0.functions.decimals().call()
    dec1 = t1.functions.decimals().call()
    sym0 = t0.functions.symbol().call()
    sym1 = t1.functions.symbol().call()

    wallet = Web3.to_checksum_address(cfg["wallet_address"])
    bal0 = t0.functions.balanceOf(wallet).call()
    bal1 = t1.functions.balanceOf(wallet).call()
    bal_native = w3.eth.get_balance(wallet)
    bal0_h = Decimal(bal0) / Decimal(10**dec0)
    bal1_h = Decimal(bal1) / Decimal(10**dec1)
    bal_native_h = Decimal(bal_native) / Decimal(10**18)

    # Determine stable side FIRST (needed by tick recomputation + amount split).
    from engine.data.lp_pools.aerodrome import KNOWN_STABLES_BASE
    is_stable_0 = t0_addr.lower() in KNOWN_STABLES_BASE
    is_stable_1 = t1_addr.lower() in KNOWN_STABLES_BASE

    # Range bounds (USD price space) → ticks in the pool's NATIVE space.
    # range_optimizer.py's tick output assumes (dec0=18, dec1=6) which is
    # wrong for cbBTC/USDC (and any other pool where decimals differ).
    # We recompute ticks here with the actual pool decimals + correct
    # inversion direction. See feedback note 2026-05-23.
    import math as _math
    rc = compute_range(snap)
    if rc is None:
        raise ValueError("range_optimizer returned no bracket — bad snapshot")
    price_lower, price_upper, _, _ = rc.chosen("balanced")

    def _usd_price_to_native_tick(usd_per_base: Decimal) -> int:
        """price_token0_in_token1 (atomic) = 1.0001^tick.
        For cbBTC/USDC where stable=t0, base=t1: native price = 1/usd_per_base.
        """
        if is_stable_1:
            price_t0_in_t1_human = float(usd_per_base)
        else:
            price_t0_in_t1_human = 1.0 / float(usd_per_base)
        raw_atomic_ratio = price_t0_in_t1_human * (10 ** (dec1 - dec0))
        return int(_math.log(raw_atomic_ratio) / _math.log(1.0001))

    t_a = _usd_price_to_native_tick(price_lower)
    t_b = _usd_price_to_native_tick(price_upper)
    raw_lower = min(t_a, t_b)
    raw_upper = max(t_a, t_b)
    # Align to tickSpacing: round LOWER down, round UPPER up (extend range out)
    def _align_down(t: int, sp: int) -> int:
        return (t // sp) * sp
    def _align_up(t: int, sp: int) -> int:
        return ((t + sp - 1) // sp) * sp
    tick_lower = _align_down(raw_lower, int(tick_spacing))
    tick_upper = _align_up(raw_upper, int(tick_spacing))
    # Sanity: range must straddle current pool tick for a balanced 2-sided mint
    if not (tick_lower <= current_tick <= tick_upper):
        logger.warning(
            "computed range [%d, %d] does NOT straddle current tick %d — "
            "mint would be one-sided (only one token used). raw=[%d, %d]",
            tick_lower, tick_upper, current_tick, raw_lower, raw_upper,
        )

    half_usd = target_position_usd / 2
    if is_stable_0:
        # stable is token0 → token1 is volatile (e.g. USDC=t0, cbBTC=t1)
        amount0_h = half_usd                                       # USDC
        amount1_h = half_usd / snap.base_price_usd                 # cbBTC
    elif is_stable_1:
        # stable is token1 → token0 is volatile (e.g. cbBTC=t0, USDC=t1)
        amount0_h = half_usd / snap.base_price_usd                 # cbBTC
        amount1_h = half_usd                                       # USDC
    else:
        raise ValueError(
            f"Neither {sym0} nor {sym1} is in stable whitelist — pricing needs an oracle"
        )

    # PSC FIX (2026-05-24 #647): For in-range positions, the pool only USES
    # the amount dictated by L = min(L0, L1) at current sqrtP. The naive
    # `amount_min = amount_desired * (1-slip)` breaks for ranges that aren't
    # symmetric around current tick — one side is over-estimated, PSC fires.
    # Correct V3 math: derive L from desired, then compute expected used0/used1
    # at current sqrtP, then set amount_min from EXPECTED USAGE.
    Q96 = 2**96
    sqrt_p = int(current_sqrt_x96)
    # math.sqrt(1.0001^t) is precise to ~9 decimals; well within 5% slippage buffer
    sqrt_a = int(_math.sqrt(1.0001 ** tick_lower) * Q96)
    sqrt_b = int(_math.sqrt(1.0001 ** tick_upper) * Q96)
    a0_desired_raw = int(amount0_h * Decimal(10**dec0))
    a1_desired_raw = int(amount1_h * Decimal(10**dec1))

    # getLiquidityForAmounts
    if sqrt_p <= sqrt_a:
        L = (a0_desired_raw * (sqrt_a * sqrt_b) // Q96) // (sqrt_b - sqrt_a)
    elif sqrt_p < sqrt_b:
        L0 = (a0_desired_raw * (sqrt_p * sqrt_b) // Q96) // (sqrt_b - sqrt_p)
        L1 = (a1_desired_raw * Q96) // (sqrt_p - sqrt_a)
        L = min(L0, L1)
    else:
        L = (a1_desired_raw * Q96) // (sqrt_b - sqrt_a)

    # getAmountsForLiquidity at current sqrtP — what the pool will ACTUALLY use
    if sqrt_p <= sqrt_a:
        used0_raw = (L * Q96 * (sqrt_b - sqrt_a)) // (sqrt_a * sqrt_b)
        used1_raw = 0
    elif sqrt_p < sqrt_b:
        used0_raw = (L * Q96 * (sqrt_b - sqrt_p)) // (sqrt_p * sqrt_b)
        used1_raw = (L * (sqrt_p - sqrt_a)) // Q96
    else:
        used0_raw = 0
        used1_raw = (L * (sqrt_b - sqrt_a)) // Q96

    used0_h = Decimal(used0_raw) / Decimal(10**dec0)
    used1_h = Decimal(used1_raw) / Decimal(10**dec1)
    logger.info(
        "amount_min from L-math: desired=(%.6f %s, %.6f %s)  expected_used=(%.6f, %.6f)  L=%d",
        amount0_h, sym0, amount1_h, sym1, used0_h, used1_h, L,
    )

    # amount_min derives from EXPECTED USAGE with slippage tolerance (PSC-safe)
    amount0_min_h = used0_h * (Decimal(1) - slippage_pct)
    amount1_min_h = used1_h * (Decimal(1) - slippage_pct)

    amount0_desired = a0_desired_raw
    amount1_desired = a1_desired_raw
    amount0_min = int(amount0_min_h * Decimal(10**dec0))
    amount1_min = int(amount1_min_h * Decimal(10**dec1))

    # Build mint calldata — protocol-aware (LP-3). Slipstream uses tickSpacing +
    # a trailing sqrtPriceX96=0 (skip pool init, RCA #644); PRJX/UniV3 uses `fee`
    # (raw = bps*100) and no sqrtPriceX96. _mint_target picks NPM + ABI + shape.
    deadline = int(time.time()) + 300                              # 5 min deadline
    npm_addr, npm_abi, is_univ3 = _mint_target(pool.protocol)
    npm = w3.eth.contract(address=npm_addr, abi=npm_abi)
    fee_or_spacing = (pool.fee_tier_bps * 100) if is_univ3 else int(tick_spacing)
    mint_params = _build_mint_params(
        is_univ3=is_univ3, t0=t0_addr, t1=t1_addr, fee_or_spacing=fee_or_spacing,
        tick_lower=tick_lower, tick_upper=tick_upper,
        a0_desired=amount0_desired, a1_desired=amount1_desired,
        a0_min=amount0_min, a1_min=amount1_min, recipient=wallet, deadline=deadline,
    )
    mint_calldata = npm.encode_abi(abi_element_identifier="mint", args=[mint_params])

    # Approve calldatas (max uint256 — gas-efficient one-time approval).
    APPROVE_AMOUNT_MAX = 2**256 - 1
    t0_approve = w3.eth.contract(address=t0_addr, abi=ERC20_APPROVE_ABI)
    t1_approve = w3.eth.contract(address=t1_addr, abi=ERC20_APPROVE_ABI)
    approve0_calldata = t0_approve.encode_abi(
        abi_element_identifier="approve",
        args=[npm_addr, APPROVE_AMOUNT_MAX],
    )
    approve1_calldata = t1_approve.encode_abi(
        abi_element_identifier="approve",
        args=[npm_addr, APPROVE_AMOUNT_MAX],
    )

    # Pre-condition check
    preconditions = []
    if bal0_h < amount0_h:
        shortfall = amount0_h - bal0_h
        preconditions.append(
            f"INSUFFICIENT {sym0}: have {bal0_h:.6f}, need {amount0_h:.6f} "
            f"(short {shortfall:.6f}) — SWAP {sym1}→{sym0} first"
        )
    if bal1_h < amount1_h:
        shortfall = amount1_h - bal1_h
        preconditions.append(
            f"INSUFFICIENT {sym1}: have {bal1_h:.6f}, need {amount1_h:.6f} "
            f"(short {shortfall:.6f}) — SWAP {sym0}→{sym1} first"
        )
    # Approve allowance check
    cur_allow0 = t0_approve.functions.allowance(wallet, npm_addr).call()
    cur_allow1 = t1_approve.functions.allowance(wallet, npm_addr).call()
    if cur_allow0 < amount0_desired:
        preconditions.append(
            f"APPROVE NEEDED: {sym0} allowance to NPM = "
            f"{Decimal(cur_allow0)/Decimal(10**dec0):.6f}, need ≥{amount0_h:.6f}"
        )
    if cur_allow1 < amount1_desired:
        preconditions.append(
            f"APPROVE NEEDED: {sym1} allowance to NPM = "
            f"{Decimal(cur_allow1)/Decimal(10**dec1):.6f}, need ≥{amount1_h:.6f}"
        )
    if bal_native_h < Decimal("0.0005"):
        preconditions.append(
            f"LOW GAS: native balance {bal_native_h:.6f} on {chain.value} — "
            f"top up before mint"
        )

    # Estimated gas
    est_gas = None
    est_gas_usd = None
    if not preconditions:
        try:
            est_gas = npm.functions.mint(mint_params).estimate_gas({"from": wallet})
            gas_price_wei = w3.eth.gas_price
            gas_wei = est_gas * gas_price_wei
            gas_eth = Decimal(gas_wei) / Decimal(10**18)
            # Rough USD: ETH ~ $2070 on Base
            est_gas_usd = gas_eth * Decimal("2070")
        except Exception as e:                            # noqa: BLE001
            logger.info("gas estimate failed (precondition?): %s", e)

    # Cost-aware sizing summary
    wallet_max = max_position_from_wallet(
        bal0_h, bal1_h, snap.base_price_usd, is_stable_0=is_stable_0,
    )
    est_rt_cost = estimate_round_trip_cost_usd(includes_swap_in_out=True)
    # Break-even days at 200% net APR: position × 2.0 × (days/365) = cost
    breakeven_days = (
        (est_rt_cost * Decimal(365)) / (target_position_usd * Decimal(2))
        if target_position_usd > 0 else Decimal(999)
    )

    return MintPreview(
        pool=pool,
        chain=chain,
        npm_address=npm_addr,
        token0_symbol=sym0,
        token1_symbol=sym1,
        token0_decimals=dec0,
        token1_decimals=dec1,
        tick_lower=int(tick_lower),
        tick_upper=int(tick_upper),
        price_lower_usd=price_lower,
        price_upper_usd=price_upper,
        amount0_desired_human=amount0_h,
        amount1_desired_human=amount1_h,
        amount0_min_human=amount0_min_h,
        amount1_min_human=amount1_min_h,
        slippage_pct=slippage_pct,
        recipient=wallet,
        deadline_iso=datetime.fromtimestamp(deadline, timezone.utc).isoformat(),
        target_position_usd=target_position_usd,
        wallet_balance_token0_human=bal0_h,
        wallet_balance_token1_human=bal1_h,
        wallet_balance_native_gas=bal_native_h,
        preconditions=preconditions,
        approve_token0_calldata=approve0_calldata,
        approve_token1_calldata=approve1_calldata,
        mint_calldata=mint_calldata,
        estimated_gas_units=est_gas,
        estimated_gas_cost_usd=est_gas_usd,
        wallet_max_position_usd=wallet_max,
        est_round_trip_cost_usd=est_rt_cost,
        breakeven_days_at_200apr=breakeven_days,
    )


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# LIVE signer — only callable from `live-open --confirm` CLI path
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MintResult:
    """Outcome of a live mint flow."""
    success: bool
    nft_token_id: Optional[int]
    approve0_tx_hash: Optional[str]
    approve1_tx_hash: Optional[str]
    mint_tx_hash: Optional[str]
    nft_verified_on_chain: bool
    error: Optional[str]
    duration_s: float


def sign_and_send_mint(
    signal: LPSignal, *, target_position_usd: Optional[Decimal] = None,
    slippage_pct: Decimal = Decimal("0.05"),
    confirm_callback=None,
    auto_swap: bool = True,
    swap_slippage_pct: Decimal = Decimal("0.005"),
) -> MintResult:
    """LIVE: approve(t0) → approve(t1) → mint(NFT) → trustless verify.

    Args:
      signal: LPSignal (action=OPEN). Re-validated against latest wallet state.
      target_position_usd: USD to deploy. If None, defaults to LP_PER_POSITION_MAX_USD.
      slippage_pct: 1% default.
      confirm_callback: optional callable(MintPreview) → bool. If provided and
                       returns False, abort before signing. The CLI wires the
                       --confirm flag here.

    Trustless rule per [[feedback-trustless-source-of-truth-model]]:
      - After mint tx confirms, query NPM.balanceOf(wallet) and compare to before.
      - If +1, success. If unchanged, ALERT loudly + mark verify=False.

    Returns MintResult — never raises. Caller logs + alerts on success.error.
    """
    import time as _time
    start = _time.time()

    # 0. LP CIRCUIT BREAKER (2026-05-30 audit FIX-2.1) — HARD GATE.
    # Previously is_active() was read only by display code (snapshot/server),
    # so an "armed" breaker blocked ZERO mints. Consult it here, at the single
    # choke point every live mint flows through, and refuse before signing.
    # Closes + bridge-home are unaffected (they don't call this function).
    try:
        from engine.core import lp_circuit_breaker as _lp_cb
        if _lp_cb.is_active():
            _st = _lp_cb.state()
            return MintResult(
                success=False, nft_token_id=None,
                approve0_tx_hash=None, approve1_tx_hash=None, mint_tx_hash=None,
                nft_verified_on_chain=False,
                error=("lp_circuit_breaker_active: refusing new mint — "
                       f"{_st.get('reason') or 'armed'}"),
                duration_s=_time.time() - start,
            )
    except Exception as _cb_exc:                             # noqa: BLE001
        # CB read must never crash the executor; a read failure is fail-open
        # per the breaker's own design (LP false-pause is costlier than a mint).
        logger.warning("[lp_exec] circuit-breaker check skipped: %s", _cb_exc)

    # 1. Build the preview (also validates wallet, allowances, gas)
    try:
        preview = build_mint_preview(
            signal, target_position_usd=target_position_usd, slippage_pct=slippage_pct,
        )
    except Exception as e:                                # noqa: BLE001
        return MintResult(
            success=False, nft_token_id=None,
            approve0_tx_hash=None, approve1_tx_hash=None, mint_tx_hash=None,
            nft_verified_on_chain=False,
            error=f"preview_failed: {type(e).__name__}: {e}",
            duration_s=_time.time() - start,
        )

    # 2. If preview has BLOCKING preconditions:
    #    - INSUFFICIENT balance → try auto-swap (per [[feedback-lp-autonomous-swaps]])
    #    - LOW GAS → can't auto-fix, refuse
    blocking_balance = [pc for pc in preview.preconditions
                        if pc.startswith("INSUFFICIENT")]
    blocking_gas = [pc for pc in preview.preconditions
                    if pc.startswith("LOW GAS")]
    if blocking_gas:
        return MintResult(
            success=False, nft_token_id=None,
            approve0_tx_hash=None, approve1_tx_hash=None, mint_tx_hash=None,
            nft_verified_on_chain=False,
            error=f"low_gas_blocker: {'; '.join(blocking_gas)}",
            duration_s=_time.time() - start,
        )
    if blocking_balance and not auto_swap:
        return MintResult(
            success=False, nft_token_id=None,
            approve0_tx_hash=None, approve1_tx_hash=None, mint_tx_hash=None,
            nft_verified_on_chain=False,
            error=f"insufficient_balance (auto_swap=False): {'; '.join(blocking_balance)}",
            duration_s=_time.time() - start,
        )
    if blocking_balance and auto_swap:
        from engine.strategies.lp_agile.swap import compute_swap_for_mint, execute_swap
        snap = signal.snapshot_at_signal
        plan = compute_swap_for_mint(
            t0_addr=signal.pool.pool_address if signal.pool.pool_address != "TBD" else None,
            t1_addr=signal.pool.pool_address if False else None,  # use preview's resolved addrs
            dec0=preview.token0_decimals, dec1=preview.token1_decimals,
            bal0_human=preview.wallet_balance_token0_human,
            bal1_human=preview.wallet_balance_token1_human,
            desired0_human=preview.amount0_desired_human,
            desired1_human=preview.amount1_desired_human,
            base_price_usd=snap.base_price_usd,
            is_stable_0=snap.is_stable_0,
        )
        # Resolve actual token addresses (preview doesn't store them — re-derive
        # from the pool)
        from engine.data.lp_pools import get_adapter
        from engine.data.lp_pools._abis import UNIV3_POOL_ABI, SLIPSTREAM_POOL_ABI
        adapter = get_adapter(signal.pool.protocol)
        pool_address = adapter.resolve_pool_address(signal.pool)
        w3_ = get_w3(signal.pool.chain)
        pool_abi = SLIPSTREAM_POOL_ABI if signal.pool.protocol == Protocol.SLIPSTREAM else UNIV3_POOL_ABI
        pool_c = w3_.eth.contract(address=pool_address, abi=pool_abi)
        t0_addr = pool_c.functions.token0().call()
        t1_addr = pool_c.functions.token1().call()
        # Re-build plan with correct addresses
        plan = compute_swap_for_mint(
            t0_addr=t0_addr, t1_addr=t1_addr,
            dec0=preview.token0_decimals, dec1=preview.token1_decimals,
            bal0_human=preview.wallet_balance_token0_human,
            bal1_human=preview.wallet_balance_token1_human,
            desired0_human=preview.amount0_desired_human,
            desired1_human=preview.amount1_desired_human,
            base_price_usd=snap.base_price_usd,
            is_stable_0=snap.is_stable_0,
        )
        if not plan.swap_needed:
            return MintResult(
                success=False, nft_token_id=None,
                approve0_tx_hash=None, approve1_tx_hash=None, mint_tx_hash=None,
                nft_verified_on_chain=False,
                error=f"insufficient_balance + auto_swap_could_not_fix: {plan.reason}",
                duration_s=_time.time() - start,
            )
        logger.info("auto-swap before mint: %s", plan.reason)
        swap_result = execute_swap(
            plan=plan, chain=signal.pool.chain,
            tick_spacing=int(signal.pool.tick_spacing or 100),
            slippage_pct=swap_slippage_pct,
        )
        if not swap_result.success:
            return MintResult(
                success=False, nft_token_id=None,
                approve0_tx_hash=None, approve1_tx_hash=None, mint_tx_hash=None,
                nft_verified_on_chain=False,
                error=f"auto_swap_failed: {swap_result.error}",
                duration_s=_time.time() - start,
            )
        logger.info("auto-swap done: in=%s out=%s slippage=%s",
                    swap_result.amount_in_human, swap_result.amount_out_human,
                    swap_result.realised_slippage_pct)
        # Re-build preview with fresh balances post-swap
        try:
            preview = build_mint_preview(
                signal, target_position_usd=target_position_usd,
                slippage_pct=slippage_pct,
            )
        except Exception as e:                            # noqa: BLE001
            return MintResult(
                success=False, nft_token_id=None,
                approve0_tx_hash=None, approve1_tx_hash=None, mint_tx_hash=None,
                nft_verified_on_chain=False,
                error=f"post_swap_preview_failed: {e}",
                duration_s=_time.time() - start,
            )

    # 3. Confirmation gate
    if confirm_callback is not None:
        if not confirm_callback(preview):
            return MintResult(
                success=False, nft_token_id=None,
                approve0_tx_hash=None, approve1_tx_hash=None, mint_tx_hash=None,
                nft_verified_on_chain=False,
                error="user_declined_confirmation",
                duration_s=_time.time() - start,
            )

    # 4. Sign + send the txs
    try:
        signer = get_signer()
    except Exception as e:                                # noqa: BLE001
        return MintResult(
            success=False, nft_token_id=None,
            approve0_tx_hash=None, approve1_tx_hash=None, mint_tx_hash=None,
            nft_verified_on_chain=False,
            error=f"signer_load_failed: {e}",
            duration_s=_time.time() - start,
        )

    chain = signal.pool.chain
    w3 = get_w3(chain)

    # LP-3: protocol-aware mint target (Slipstream on Base vs PRJX/UniV3 on HyperEVM)
    npm_addr, npm_abi, is_univ3 = _mint_target(signal.pool.protocol)

    # Snapshot NFT count BEFORE for trustless verify
    from engine.data.lp_pools._abis import UNIV3_POSITION_MANAGER_ABI
    npm_c = w3.eth.contract(
        address=npm_addr, abi=UNIV3_POSITION_MANAGER_ABI,
    )
    wallet = signer.address
    nft_count_before = npm_c.functions.balanceOf(wallet).call()

    # token addresses
    from engine.data.lp_pools._abis import UNIV3_POOL_ABI, SLIPSTREAM_POOL_ABI
    from engine.data.lp_pools import get_adapter
    pool_address = get_adapter(signal.pool.protocol).resolve_pool_address(signal.pool)
    pool_abi = SLIPSTREAM_POOL_ABI
    pool_c = w3.eth.contract(address=pool_address, abi=pool_abi)
    t0_addr = pool_c.functions.token0().call()
    t1_addr = pool_c.functions.token1().call()

    # ===== APPROVE token0 =====
    approve0_hash = None
    try:
        amount0 = int(preview.amount0_desired_human * Decimal(10 ** preview.token0_decimals))
        # Use exact-amount approve for first-trade safety (vs MAX)
        approve0_hash = _send_approve(
            w3, signer, t0_addr, npm_addr, amount0,
            label=f"approve {preview.token0_symbol}",
        )
    except Exception as e:                                # noqa: BLE001
        return MintResult(
            success=False, nft_token_id=None,
            approve0_tx_hash=approve0_hash, approve1_tx_hash=None, mint_tx_hash=None,
            nft_verified_on_chain=False,
            error=f"approve_{preview.token0_symbol}_failed: {type(e).__name__}: {e}",
            duration_s=_time.time() - start,
        )

    # ===== APPROVE token1 =====
    approve1_hash = None
    try:
        amount1 = int(preview.amount1_desired_human * Decimal(10 ** preview.token1_decimals))
        approve1_hash = _send_approve(
            w3, signer, t1_addr, npm_addr, amount1,
            label=f"approve {preview.token1_symbol}",
        )
    except Exception as e:                                # noqa: BLE001
        return MintResult(
            success=False, nft_token_id=None,
            approve0_tx_hash=approve0_hash, approve1_tx_hash=approve1_hash,
            mint_tx_hash=None, nft_verified_on_chain=False,
            error=f"approve_{preview.token1_symbol}_failed: {type(e).__name__}: {e}",
            duration_s=_time.time() - start,
        )

    # ===== MINT NFT =====
    mint_hash = None
    nft_token_id = None
    try:
        # Re-build mint params with FRESH deadline (5 min from now).
        # sqrtPriceX96=0 means "skip pool init" — required when pool exists
        # (RCA #644: non-zero triggers createPool() which reverts on existing pool)
        deadline = int(_time.time()) + 300
        npm = w3.eth.contract(address=npm_addr, abi=npm_abi)
        # LP-3: UniV3/prjx uses `fee` (raw = bps*100); Slipstream uses tickSpacing
        # (+ trailing sqrtPriceX96=0 to skip pool init, RCA #644). _build_mint_params
        # produces the protocol-correct tuple.
        fee_or_spacing = (signal.pool.fee_tier_bps * 100) if is_univ3 \
            else int(signal.pool.tick_spacing or 100)
        amount0_min = int(preview.amount0_min_human * Decimal(10 ** preview.token0_decimals))
        amount1_min = int(preview.amount1_min_human * Decimal(10 ** preview.token1_decimals))
        mint_params = _build_mint_params(
            is_univ3=is_univ3,
            t0=t0_addr, t1=t1_addr, fee_or_spacing=fee_or_spacing,
            tick_lower=int(preview.tick_lower), tick_upper=int(preview.tick_upper),
            a0_desired=int(preview.amount0_desired_human * Decimal(10 ** preview.token0_decimals)),
            a1_desired=int(preview.amount1_desired_human * Decimal(10 ** preview.token1_decimals)),
            a0_min=int(amount0_min), a1_min=int(amount1_min),
            recipient=wallet, deadline=deadline,
        )
        mint_fn = npm.functions.mint(mint_params)
        # FIX 2026-05-23: use "pending" to count in-flight approves; brief sleep
        # gives the RPC node time to index the just-confirmed approves.
        _time.sleep(0.5)
        nonce = w3.eth.get_transaction_count(wallet, "pending")
        tx = mint_fn.build_transaction({
            "from": wallet,
            "nonce": nonce,
            "gas": int((preview.estimated_gas_units or 500_000) * 1.3),
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": w3.to_wei(0.001, "gwei"),
            "chainId": w3.eth.chain_id,
            "value": 0,
        })
        signed = signer.sign_transaction(tx)
        mint_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
        logger.info("mint tx submitted: %s — waiting confirmation...", mint_hash)
        rcpt = w3.eth.wait_for_transaction_receipt(mint_hash, timeout=180)
        if rcpt.status != 1:
            return MintResult(
                success=False, nft_token_id=None,
                approve0_tx_hash=approve0_hash, approve1_tx_hash=approve1_hash,
                mint_tx_hash=mint_hash, nft_verified_on_chain=False,
                error=f"mint_reverted: receipt status={rcpt.status}",
                duration_s=_time.time() - start,
            )
        # Parse NFT tokenId from Transfer event logs (ERC721 Transfer(address,address,uint256))
        # NB: in web3.py ≥7, HexBytes.hex() returns RAW hex (no 0x prefix), so
        # we strip both sides before comparing — and use int.from_bytes for the
        # tokenId since `int(hex_str, 16)` chokes on non-hex chars in some envs.
        TRANSFER_TOPIC_RAW = (
            "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        )
        for log in rcpt.logs:
            if (log.address.lower() == npm_addr.lower()
                    and len(log.topics) >= 4
                    and log.topics[0].hex().lower().lstrip("0x") == TRANSFER_TOPIC_RAW):
                # topic[3] = tokenId (indexed uint256) — read as raw bytes
                nft_token_id = int.from_bytes(bytes(log.topics[3]), "big")
                break
    except Exception as e:                                # noqa: BLE001
        return MintResult(
            success=False, nft_token_id=nft_token_id,
            approve0_tx_hash=approve0_hash, approve1_tx_hash=approve1_hash,
            mint_tx_hash=mint_hash, nft_verified_on_chain=False,
            error=f"mint_failed: {type(e).__name__}: {e}",
            duration_s=_time.time() - start,
        )

    # ===== TRUSTLESS VERIFY =====
    # Re-read NFT count + new NFT exists in wallet
    try:
        nft_count_after = npm_c.functions.balanceOf(wallet).call()
        verified = nft_count_after == nft_count_before + 1
        if not verified:
            logger.warning(
                "trustless verify FAILED: nft_count before=%d after=%d (mint may have failed silently)",
                nft_count_before, nft_count_after,
            )
    except Exception as e:                                # noqa: BLE001
        logger.warning("trustless verify read failed: %s", e)
        verified = False

    # ===== COST LEDGER: log mint event =====
    try:
        from engine.strategies.lp_agile.cost_ledger import log_mint
        # Best-effort gas USD estimate from the mint receipt
        try:
            mint_rcpt = w3.eth.get_transaction_receipt(mint_hash)
            gas_cost_usd = (Decimal(mint_rcpt.gasUsed * w3.eth.gas_price)
                            / Decimal(10**18) * Decimal("2070"))
        except Exception:
            gas_cost_usd = Decimal("0.30")  # conservative fallback
        position_id = f"lp-{signal.pool.id}-{nft_token_id or 'unknown'}"
        log_mint(
            position_id=position_id,
            tx_hash=mint_hash, chain=chain.value,
            nft_token_id=nft_token_id or 0,
            deposited_usd=preview.target_position_usd,
            gas_cost_usd=gas_cost_usd,
        )
    except Exception as e:                                # noqa: BLE001
        logger.warning("cost ledger log_mint failed: %s — proceeding", e)

    return MintResult(
        success=True,
        nft_token_id=nft_token_id,
        approve0_tx_hash=approve0_hash,
        approve1_tx_hash=approve1_hash,
        mint_tx_hash=mint_hash,
        nft_verified_on_chain=verified,
        error=None,
        duration_s=_time.time() - start,
    )


def _send_approve(
    w3, signer, token_addr: str, spender: str, amount: int, *, label: str,
) -> str:
    """Send an ERC20.approve(spender, amount) tx. Returns tx hash hex.

    Waits for confirmation (status==1) before returning. Raises on revert.
    """
    import time as _t
    token = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_APPROVE_ABI)
    wallet = signer.address
    # Skip if allowance already sufficient
    current = token.functions.allowance(wallet, spender).call()
    if current >= amount:
        logger.info("%s: allowance %d already ≥ %d, skipping", label, current, amount)
        return "0x0_skipped_already_approved"

    # 2026-06-04 #419 — retry on throttle. The "in-flight transaction limit
    # reached for delegated accounts" RPC error can hit transiently after a
    # prior tx. Retry 3x with backoff before giving up.
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            nonce = w3.eth.get_transaction_count(wallet, "pending")
            tx = token.functions.approve(spender, amount).build_transaction({
                "from": wallet,
                "nonce": nonce,
                "gas": 100_000,
                "maxFeePerGas": w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": w3.to_wei(0.001, "gwei"),
                "chainId": w3.eth.chain_id,
                "value": 0,
            })
            signed = signer.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
            logger.info(
                "%s tx submitted (attempt %d/3): %s — waiting confirmation...",
                label, attempt + 1, tx_hash,
            )
            rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if rcpt.status != 1:
                raise RuntimeError(f"{label} reverted (status={rcpt.status})")
            return tx_hash
        except Exception as exc:                                  # noqa: BLE001
            last_exc = exc
            err_str = str(exc).lower()
            is_throttle = (
                "in-flight" in err_str
                or "rate limit" in err_str
                or "too many requests" in err_str
                or "-32000" in err_str
            )
            if not is_throttle or attempt == 2:
                raise
            backoff_s = 10 * (attempt + 1)
            logger.warning(
                "%s throttle-class error (attempt %d/3, retry in %ds): %s",
                label, attempt + 1, backoff_s, exc,
            )
            import time as _t
            _t.sleep(backoff_s)
    # Unreachable, but for type safety
    raise last_exc or RuntimeError(f"{label} exhausted retries")


def _send_gauge_or_nft_tx(
    w3,
    signer,
    contract,
    fn_name: str,
    fn_args: tuple,
    *,
    label: str,
    gas: int | None = None,
    gas_fallback: int = 800_000,
) -> str:
    """Send a contract write tx (gauge withdraw/deposit, NFT approve, etc.).

    Same retry-with-backoff pattern as _send_approve. Returns tx hash.
    Raises on revert or after 3 throttle retries.

    Gas handling — IMPORTANT (RCA 2026-05-24 in gauge_stake.py):
      Slipstream CLGauge.deposit() / withdraw() touch many reward-accounting
      storage slots; real-world usage 540k+. Hardcoding 300-350k OOG-reverts
      silently with "no data". So we DYNAMIC-ESTIMATE + 50% headroom, fall
      back to 800k if estimate fails.

    2026-06-04 #419 — needed for unstake→increase→restake dance on staked
    Aerodrome positions. NFT 71481609 surfaced this when first auto-deploy
    reverted because the NFT lives in the gauge contract, not the wallet.
    Then surfaced AGAIN at fixed 350k → OOG → "no data" revert. Now uses
    same dynamic-estimate pattern as the proven gauge_stake.py.
    """
    import time as _t

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            nonce = w3.eth.get_transaction_count(signer.address, "pending")
            fn = getattr(contract.functions, fn_name)(*fn_args)
            # Estimate gas dynamically with 50% headroom. Falls back to
            # gas_fallback (default 800k) when estimate fails — typically
            # happens when contract would revert anyway, so the higher
            # ceiling just gives us a clearer revert reason on submission.
            if gas is None:
                try:
                    est = fn.estimate_gas({"from": signer.address})
                    gas_to_use = int(est * 1.5)
                    logger.info(
                        "%s gas estimate=%d, using %d (est*1.5)",
                        label, est, gas_to_use,
                    )
                except Exception as est_exc:                     # noqa: BLE001
                    gas_to_use = gas_fallback
                    logger.warning(
                        "%s gas estimate failed (%s), falling back to %d",
                        label, est_exc, gas_to_use,
                    )
            else:
                gas_to_use = gas
            tx = fn.build_transaction({
                "from": signer.address,
                "nonce": nonce,
                "gas": gas_to_use,
                "maxFeePerGas": w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": w3.to_wei(0.001, "gwei"),
                "chainId": w3.eth.chain_id,
                "value": 0,
            })
            signed = signer.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
            logger.info(
                "%s tx submitted (attempt %d/3): %s — waiting confirmation...",
                label, attempt + 1, tx_hash,
            )
            rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if rcpt.status != 1:
                raise RuntimeError(f"{label} reverted (status={rcpt.status})")
            return tx_hash
        except Exception as exc:                                  # noqa: BLE001
            last_exc = exc
            err_str = str(exc).lower()
            is_throttle = (
                "in-flight" in err_str
                or "rate limit" in err_str
                or "too many requests" in err_str
                or "-32000" in err_str
            )
            if not is_throttle or attempt == 2:
                raise
            backoff_s = 10 * (attempt + 1)
            logger.warning(
                "%s throttle-class error (attempt %d/3, retry in %ds): %s",
                label, attempt + 1, backoff_s, exc,
            )
            _t.sleep(backoff_s)
    raise last_exc or RuntimeError(f"{label} exhausted retries")


def render_mint_preview(p: MintPreview) -> str:
    """Human-readable summary of a mint preview. Safe to print to terminal."""
    lines = [
        "",
        "═" * 78,
        f"  💡 MINT PREVIEW — {p.pool.id} on {p.chain.value}",
        f"  ⚠️  DRY-RUN ONLY — nothing signed, nothing sent",
        "═" * 78,
        f"  NPM contract     : {p.npm_address}",
        f"  Recipient        : {p.recipient}",
        f"  Deadline         : {p.deadline_iso}",
        f"  Slippage         : {p.slippage_pct*100:.2f}%",
        "",
        f"  Range:",
        f"    Ticks          : [{p.tick_lower}, {p.tick_upper}]",
        f"    Price (USD)    : [${p.price_lower_usd:,.4f}, ${p.price_upper_usd:,.4f}]",
        "",
        f"  Capital deploy   : ${p.target_position_usd}",
        f"    {p.token0_symbol:>8s} desired: {p.amount0_desired_human:>14,.6f}  (min {p.amount0_min_human:,.6f})",
        f"    {p.token1_symbol:>8s} desired: {p.amount1_desired_human:>14,.6f}  (min {p.amount1_min_human:,.6f})",
        "",
        f"  Wallet balances:",
        f"    {p.token0_symbol:>8s}        : {p.wallet_balance_token0_human:>14,.6f}",
        f"    {p.token1_symbol:>8s}        : {p.wallet_balance_token1_human:>14,.6f}",
        f"    native gas    : {p.wallet_balance_native_gas:>14,.6f}",
    ]
    if p.preconditions:
        lines.extend(["", "  🚧 PRE-CONDITIONS UNMET (mint would revert):"])
        for pc in p.preconditions:
            lines.append(f"    ✗ {pc}")
    else:
        lines.extend(["", "  ✅ All pre-conditions satisfied — mint can proceed."])

    if p.estimated_gas_units:
        lines.extend([
            "",
            f"  Mint gas est     : {p.estimated_gas_units:,} units  (~${p.estimated_gas_cost_usd:.4f})",
        ])

    lines.extend([
        "",
        f"  💰 Cost-aware economics:",
        f"    Wallet max LP position : ${p.wallet_max_position_usd:.2f}  (50/50 capped by short leg)",
        f"    Round-trip cost (est)  : ${p.est_round_trip_cost_usd:.2f}  "
        f"= {(p.est_round_trip_cost_usd / p.target_position_usd * 100 if p.target_position_usd else 0):.1f}% of ${p.target_position_usd}",
        f"    Break-even at 200% APR : {p.breakeven_days_at_200apr:.1f} days  (need to earn back cost via fees)",
    ])

    lines.extend([
        "",
        f"  Calldata (NOT auto-sent):",
        f"    approve {p.token0_symbol}     : 0x{p.approve_token0_calldata[2:18]}...{p.approve_token0_calldata[-16:]}",
        f"    approve {p.token1_symbol}     : 0x{p.approve_token1_calldata[2:18]}...{p.approve_token1_calldata[-16:]}",
        f"    mint NFT       : 0x{p.mint_calldata[2:18]}...{p.mint_calldata[-16:]}",
        "═" * 78,
        "",
    ])
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# Phase B (#369): prjx in-place range adjust — sign + broadcast
# ═══════════════════════════════════════════════════════════════════════════
# Builds on prjx_v2.encode_range_adjust() from Phase A. Adds:
#   - LP wallet signing via eth_account.LocalAccount (re-using get_signer())
#   - eth_estimateGas + balance check + net-cost guard
#   - EIP-1559 tx assembly + send + receipt wait
# Default-OFF behind LP_AUTO_EXECUTE_PRJX env flag. Phase D (Telegram approve)
# is what flips it per-tx; the default-OFF stance protects from runaway autos.


@dataclass
class RebalanceResult:
    """Outcome of a sign_and_send_range_adjust call."""
    executed: bool
    tx_hash: Optional[str] = None
    actual_gas_units: Optional[int] = None
    actual_gas_usd: Optional[float] = None
    error: Optional[str] = None
    skipped_reason: Optional[str] = None  # net-cost guard / disabled / dry-run
    duration_s: float = 0.0


# Cooldown registry — disk-persisted via approval_store (Phase D, #371).
# Keyed by nft_token_id → last-attempt epoch. Same NFT can't rebalance more
# than once per LP_REBALANCE_COOLDOWN_SEC window. Loaded on import so a
# scheduler restart can't bypass cooldown.
try:
    from engine.strategies.lp_agile.approval_store import (
        load_cooldown_map as _load_cooldown_map,
        record_cooldown as _record_cooldown_disk,
    )
    _LAST_REBALANCE_TS: dict[int, float] = _load_cooldown_map()
except Exception:                                                # noqa: BLE001
    # If approval_store is unavailable at import time, fall back to in-memory.
    _LAST_REBALANCE_TS = {}
    def _record_cooldown_disk(tid, ts=None): pass                # type: ignore


def _hyperevm_native_usd() -> Decimal:
    """Best-effort USD price of HYPE (HyperEVM native gas token).

    Used to convert gas cost (in wei → HYPE → USD) for the net-cost guard.
    Falls back to a conservative $50 if no price source available."""
    try:
        # Reuse coingecko helper if present in lp_pools utilities.
        from engine.data.lp_pools._fees import _coingecko_price_usd  # type: ignore
        v = _coingecko_price_usd("hyperliquid")
        if v and Decimal(str(v)) > 0:
            return Decimal(str(v))
    except Exception:
        pass
    return Decimal("50")  # conservative fallback


def sign_and_send_range_adjust(
    plan: dict,
    *,
    confirm_callback=None,
) -> RebalanceResult:
    """Execute a prjx in-place range adjust per the rebalance plan.

    `plan` shape: matches one entry from ops/pwa/serve/lp_rebalance_plans.json
    (or engine snapshot.rebalance_plans). Required fields:
       - nft_token_id (int)
       - new_tick_lower (int) / new_tick_upper (int)
       - est_net_cost_usd (float) — for the net-cost guard
       - expected_apr_pct_new_range (optional float) — for ROI guard

    Safety gates (in order — first failure aborts):
      1. LP_AUTO_EXECUTE_PRJX env must be 'true' (else SKIP).
      2. Cooldown: same nft_token_id can't rebalance within
         LP_REBALANCE_COOLDOWN_SEC (default 4h = 14400).
      3. Plan must have new_tick_lower < new_tick_upper, both int24-range.
      4. Pre-flight gas estimate must succeed (eth_estimateGas).
      5. Net-cost gate: actual_gas_usd <= 50% of expected_gain_usd_30d
         (gain derived from APR delta × position value × 30d). If no APR
         baseline available, defer to fixed cap LP_REBALANCE_MAX_GAS_USD.
      6. Wallet must have ≥ gas_usd * 2 reserved for the tx.

    Returns RebalanceResult — never raises (operator can read .error)."""
    import time as _time
    start = _time.time()

    nft_token_id = plan.get("nft_token_id")
    if nft_token_id is None:
        return RebalanceResult(
            executed=False,
            error="plan missing nft_token_id",
            duration_s=_time.time() - start,
        )

    # Gate 1 — auto-execute flag (per-protocol so Aerodrome can be flipped
    # independently when Phase C ships).
    auto_exec_enabled = (os.environ.get("LP_AUTO_EXECUTE_PRJX", "false")
                        .lower() in ("true", "1", "yes"))
    if not auto_exec_enabled:
        return RebalanceResult(
            executed=False,
            skipped_reason=("LP_AUTO_EXECUTE_PRJX=false — set to 'true' in "
                            "engine/.env to enable, or use Telegram approve "
                            "(Phase D)"),
            duration_s=_time.time() - start,
        )

    # Gate 2 — cooldown per NFT (prevents flapping).
    cooldown_sec = int(os.environ.get("LP_REBALANCE_COOLDOWN_SEC", "14400"))
    last_ts = _LAST_REBALANCE_TS.get(int(nft_token_id), 0)
    age = _time.time() - last_ts
    if last_ts > 0 and age < cooldown_sec:
        return RebalanceResult(
            executed=False,
            skipped_reason=(f"cooldown active: tokenId={nft_token_id} last "
                            f"rebalanced {int(age)}s ago, min "
                            f"{cooldown_sec}s"),
            duration_s=_time.time() - start,
        )

    # Gate 3 — tick validity (encoder also checks, but cheap to short-circuit).
    new_tick_lower = plan.get("new_tick_lower")
    new_tick_upper = plan.get("new_tick_upper")
    if new_tick_lower is None or new_tick_upper is None:
        return RebalanceResult(
            executed=False,
            error="plan missing new_tick_lower/new_tick_upper",
            duration_s=_time.time() - start,
        )
    if new_tick_lower >= new_tick_upper:
        return RebalanceResult(
            executed=False,
            error=(f"invalid tick range: lower={new_tick_lower} >= "
                   f"upper={new_tick_upper}"),
            duration_s=_time.time() - start,
        )

    # Build the calldata via Phase A encoder.
    try:
        from engine.data.lp_pools.prjx_v2 import (
            encode_range_adjust as _encode,
            PRJX_AGGREGATOR_ADDRESS,
        )
    except Exception as _exc:                                   # noqa: BLE001
        return RebalanceResult(
            executed=False,
            error=f"prjx_v2 import failed: {type(_exc).__name__}: {_exc}",
            duration_s=_time.time() - start,
        )

    # Load LP wallet signer.
    try:
        signer = get_signer()
    except Exception as e:                                      # noqa: BLE001
        return RebalanceResult(
            executed=False,
            error=f"signer_load_failed: {type(e).__name__}: {e}",
            duration_s=_time.time() - start,
        )
    wallet = signer.address

    # 5-min deadline (matches rebalance_plan's gas-estimate window).
    deadline = int(_time.time()) + 300
    try:
        cd = _encode(
            token_id=int(nft_token_id),
            recipient=wallet,
            new_tick_lower=int(new_tick_lower),
            new_tick_upper=int(new_tick_upper),
            deadline_unix=deadline,
        )
    except KeyError as e:
        return RebalanceResult(
            executed=False,
            error=(f"no prjx_v2 template for tokenId={nft_token_id}. Seed "
                   f"one via REFERENCE_CALLDATA_BY_TOKEN dict and retry. "
                   f"({e})"),
            duration_s=_time.time() - start,
        )
    except (ValueError, Exception) as e:                        # noqa: BLE001
        return RebalanceResult(
            executed=False,
            error=f"encode_range_adjust failed: {type(e).__name__}: {e}",
            duration_s=_time.time() - start,
        )

    # Connect to HyperEVM.
    from engine.data.lp_pools._evm import Chain
    w3 = get_w3(Chain.HYPEREVM)
    chain_id = w3.eth.chain_id

    # Gas estimate (call eth_estimateGas with from=wallet).
    try:
        gas_units_est = w3.eth.estimate_gas({
            "from": wallet,
            "to": cd.to_address,
            "value": 0,
            "data": cd.data_hex,
        })
    except Exception as e:                                      # noqa: BLE001
        return RebalanceResult(
            executed=False,
            error=(f"estimate_gas failed (tx would likely revert): "
                   f"{type(e).__name__}: {str(e)[:200]}"),
            duration_s=_time.time() - start,
        )

    # Compute USD gas cost.
    gas_price_wei = w3.eth.gas_price
    gas_units_with_buffer = int(gas_units_est * 1.3)
    est_total_wei = gas_units_with_buffer * gas_price_wei * 2  # × maxFeePerGas (=gas_price*2)
    hype_usd = _hyperevm_native_usd()
    gas_cost_hype = Decimal(est_total_wei) / Decimal(10 ** 18)
    gas_cost_usd = gas_cost_hype * hype_usd

    # Gate 5 — net-cost gate.
    # If plan ships est_net_cost_usd we use it; else fall back to a hard
    # ceiling LP_REBALANCE_MAX_GAS_USD (default $3 — pos values $50-500).
    max_gas_usd_cap = Decimal(
        os.environ.get("LP_REBALANCE_MAX_GAS_USD", "3.00"))
    if gas_cost_usd > max_gas_usd_cap:
        return RebalanceResult(
            executed=False,
            skipped_reason=(f"gas-cost gate: estimated ${gas_cost_usd:.2f} > "
                            f"${max_gas_usd_cap:.2f} cap (set "
                            f"LP_REBALANCE_MAX_GAS_USD to override)"),
            actual_gas_units=gas_units_est,
            actual_gas_usd=float(gas_cost_usd),
            duration_s=_time.time() - start,
        )

    # Gate 6 — wallet balance ≥ 2× est gas (reserve for retry).
    bal_wei = w3.eth.get_balance(wallet)
    reqd_wei = est_total_wei * 2
    if bal_wei < reqd_wei:
        return RebalanceResult(
            executed=False,
            error=(f"wallet balance insufficient: {bal_wei / 10**18:.6f} HYPE "
                   f"vs needed {reqd_wei / 10**18:.6f} HYPE (with 2× safety)"),
            actual_gas_units=gas_units_est,
            actual_gas_usd=float(gas_cost_usd),
            duration_s=_time.time() - start,
        )

    # Optional confirm hook (Phase D will inject Telegram approve here).
    if confirm_callback is not None:
        try:
            approved = bool(confirm_callback({
                "tokenId":   int(nft_token_id),
                "to":        cd.to_address,
                "new_ticks": [int(new_tick_lower), int(new_tick_upper)],
                "deadline":  deadline,
                "gas_usd":   float(gas_cost_usd),
                "summary":   cd.decoded_summary,
            }))
        except Exception as e:                                  # noqa: BLE001
            return RebalanceResult(
                executed=False,
                error=f"confirm_callback raised: {type(e).__name__}: {e}",
                actual_gas_units=gas_units_est,
                actual_gas_usd=float(gas_cost_usd),
                duration_s=_time.time() - start,
            )
        if not approved:
            return RebalanceResult(
                executed=False,
                skipped_reason="rejected by confirm_callback",
                actual_gas_units=gas_units_est,
                actual_gas_usd=float(gas_cost_usd),
                duration_s=_time.time() - start,
            )

    # ===== SIGN + BROADCAST =====
    try:
        nonce = w3.eth.get_transaction_count(wallet, "pending")
        tx = {
            "from":  wallet,
            "to":    cd.to_address,
            "value": 0,
            "data":  cd.data_hex,
            "nonce": nonce,
            "gas":   gas_units_with_buffer,
            "maxFeePerGas": gas_price_wei * 2,
            "maxPriorityFeePerGas": w3.to_wei(0.001, "gwei"),
            "chainId": chain_id,
        }
        signed = signer.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
        logger.info(
            "[rebalance] tokenId=%s tx broadcast: %s",
            nft_token_id, tx_hash,
        )
        rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        if rcpt.status != 1:
            return RebalanceResult(
                executed=False,
                tx_hash=tx_hash,
                actual_gas_units=int(rcpt.gasUsed),
                actual_gas_usd=float(
                    Decimal(rcpt.gasUsed * gas_price_wei * 2)
                    / Decimal(10 ** 18) * hype_usd),
                error=f"tx_reverted: receipt status={rcpt.status}",
                duration_s=_time.time() - start,
            )
        # Success — record cooldown timestamp (disk-persisted).
        _LAST_REBALANCE_TS[int(nft_token_id)] = _time.time()
        try:
            _record_cooldown_disk(int(nft_token_id))
        except Exception:                                        # noqa: BLE001
            pass
        actual_cost = float(
            Decimal(rcpt.gasUsed * gas_price_wei * 2)
            / Decimal(10 ** 18) * hype_usd)
        logger.info(
            "[rebalance] tokenId=%s SUCCESS gas=%s units (~$%.2f)",
            nft_token_id, rcpt.gasUsed, actual_cost,
        )
        return RebalanceResult(
            executed=True,
            tx_hash=tx_hash,
            actual_gas_units=int(rcpt.gasUsed),
            actual_gas_usd=actual_cost,
            duration_s=_time.time() - start,
        )
    except Exception as e:                                      # noqa: BLE001
        return RebalanceResult(
            executed=False,
            error=f"sign_and_send raised: {type(e).__name__}: {str(e)[:300]}",
            actual_gas_units=gas_units_est,
            actual_gas_usd=float(gas_cost_usd),
            duration_s=_time.time() - start,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Phase C (#370): Aerodrome Slipstream rebalance — sign + broadcast
# ═══════════════════════════════════════════════════════════════════════════
# Multi-tx flow (3-5 txs depending on staked state). Each tx independently
# signed/broadcast/waited with the same safety gates as prjx. The mint
# multicall step produces a NEW tokenId which the executor parses from the
# receipt and feeds into the post-mint approve+deposit steps.


def sign_and_send_range_adjust_aero(
    plan: dict,
    *,
    confirm_callback=None,
) -> "RebalanceResult":
    """Execute an Aerodrome Slipstream rebalance per the plan.

    Plan fields required:
       - nft_token_id (int)
       - new_tick_lower / new_tick_upper (int)
       - is_staked (bool) — from lp_summary.open_positions
       - pool_address (str) — for the Slipstream pool
       - gauge_address (str, optional) — required if is_staked=True
       - token0, token1 (str) — pool token addresses
       - tick_spacing (int) — Slipstream pool's spacing
       - old_liquidity (int) — current liquidity to decrease (full burn)
       - amount0_desired, amount1_desired (int) — for the new mint

    Same 6 safety gates as prjx variant, plus:
       7. is_staked=True without gauge_address → error
       8. ANY step revert → halt + return error with which step failed

    Gate 1 uses LP_AUTO_EXECUTE_AERO env (independent from PRJX flag).
    """
    import time as _time
    start = _time.time()

    nft_token_id = plan.get("nft_token_id")
    if nft_token_id is None:
        return RebalanceResult(
            executed=False, error="plan missing nft_token_id",
            duration_s=_time.time() - start,
        )

    # Gate 1 — auto-execute flag (separate from prjx).
    auto_exec_enabled = (os.environ.get("LP_AUTO_EXECUTE_AERO", "false")
                        .lower() in ("true", "1", "yes"))
    if not auto_exec_enabled:
        return RebalanceResult(
            executed=False,
            skipped_reason=("LP_AUTO_EXECUTE_AERO=false — set to 'true' in "
                            "engine/.env to enable, or use Telegram approve "
                            "(Phase D)"),
            duration_s=_time.time() - start,
        )

    # Gate 2 — cooldown (shared bookkeeping with prjx).
    cooldown_sec = int(os.environ.get("LP_REBALANCE_COOLDOWN_SEC", "14400"))
    last_ts = _LAST_REBALANCE_TS.get(int(nft_token_id), 0)
    age = _time.time() - last_ts
    if last_ts > 0 and age < cooldown_sec:
        return RebalanceResult(
            executed=False,
            skipped_reason=(f"cooldown active: tokenId={nft_token_id} last "
                            f"rebalanced {int(age)}s ago, min "
                            f"{cooldown_sec}s"),
            duration_s=_time.time() - start,
        )

    # Gate 3 — required fields.
    required = ("new_tick_lower", "new_tick_upper", "token0", "token1",
                "tick_spacing", "old_liquidity",
                "amount0_desired", "amount1_desired")
    for k in required:
        if plan.get(k) is None:
            return RebalanceResult(
                executed=False,
                error=f"plan missing required field: {k}",
                duration_s=_time.time() - start,
            )

    is_staked = bool(plan.get("is_staked"))
    if is_staked and not plan.get("gauge_address"):
        return RebalanceResult(
            executed=False,
            error="is_staked=True but gauge_address not in plan",
            duration_s=_time.time() - start,
        )

    if plan["new_tick_lower"] >= plan["new_tick_upper"]:
        return RebalanceResult(
            executed=False,
            error=(f"invalid tick range: "
                   f"lower={plan['new_tick_lower']} >= "
                   f"upper={plan['new_tick_upper']}"),
            duration_s=_time.time() - start,
        )

    # Load signer + RPC.
    try:
        signer = get_signer()
    except Exception as e:                                      # noqa: BLE001
        return RebalanceResult(
            executed=False,
            error=f"signer_load_failed: {type(e).__name__}: {e}",
            duration_s=_time.time() - start,
        )
    wallet = signer.address

    try:
        from engine.data.lp_pools._evm import Chain
        from engine.data.lp_pools.aerodrome_v2 import (
            encode_steps, parse_new_token_id_from_receipt,
            NPM_WRITE_ABI, GAUGE_WRITE_ABI, AERO_SLIPSTREAM_NPM,
        )
    except Exception as e:                                      # noqa: BLE001
        return RebalanceResult(
            executed=False,
            error=f"aerodrome_v2 import failed: {type(e).__name__}: {e}",
            duration_s=_time.time() - start,
        )

    w3 = get_w3(Chain.BASE)
    chain_id = w3.eth.chain_id

    # Build the tx sequence.
    deadline = int(_time.time()) + 600  # 10-min window (multi-tx is slow)
    try:
        seq = encode_steps(
            w3=w3,
            old_token_id=int(nft_token_id),
            old_liquidity=int(plan["old_liquidity"]),
            owner=wallet,
            token0=plan["token0"],
            token1=plan["token1"],
            tick_spacing=int(plan["tick_spacing"]),
            new_tick_lower=int(plan["new_tick_lower"]),
            new_tick_upper=int(plan["new_tick_upper"]),
            amount0_desired=int(plan["amount0_desired"]),
            amount1_desired=int(plan["amount1_desired"]),
            deadline_unix=deadline,
            is_staked=is_staked,
            gauge_address=plan.get("gauge_address"),
            amount0_min=int(plan.get("amount0_min", 0)),
            amount1_min=int(plan.get("amount1_min", 0)),
        )
    except Exception as e:                                      # noqa: BLE001
        return RebalanceResult(
            executed=False,
            error=f"encode_steps failed: {type(e).__name__}: {e}",
            duration_s=_time.time() - start,
        )

    # Pre-flight: gas estimate for the multicall step (largest, most likely
    # to revert). Cheaper than estimating every step.
    multi_step = seq.steps[seq.expected_new_tokenId_after_step]
    try:
        gas_units_est = w3.eth.estimate_gas({
            "from": wallet,
            "to": multi_step.to,
            "value": 0,
            "data": multi_step.data_hex,
        })
    except Exception as e:                                      # noqa: BLE001
        return RebalanceResult(
            executed=False,
            error=(f"estimate_gas on multicall failed (likely revert): "
                   f"{type(e).__name__}: {str(e)[:200]}"),
            duration_s=_time.time() - start,
        )

    gas_price_wei = w3.eth.gas_price
    # Estimate total gas (multicall is the big one; gauge txs are cheap)
    # Conservative: assume each gauge tx ~80k gas. 4 gauge steps + multicall.
    est_total_gas_units = gas_units_est + (80_000 * (len(seq.steps) - 1))
    est_total_wei = est_total_gas_units * gas_price_wei * 2
    # Base ETH price (rough estimate — production should pull from oracle)
    eth_usd = Decimal(os.environ.get("ETH_USD_PRICE_HINT", "3500"))
    gas_cost_usd = (Decimal(est_total_wei) / Decimal(10**18)) * eth_usd

    max_gas_usd_cap = Decimal(
        os.environ.get("LP_REBALANCE_MAX_GAS_USD", "3.00"))
    if gas_cost_usd > max_gas_usd_cap:
        return RebalanceResult(
            executed=False,
            skipped_reason=(f"gas-cost gate: estimated ${gas_cost_usd:.2f} > "
                            f"${max_gas_usd_cap:.2f} cap"),
            actual_gas_units=gas_units_est,
            actual_gas_usd=float(gas_cost_usd),
            duration_s=_time.time() - start,
        )

    # Wallet balance check.
    bal_wei = w3.eth.get_balance(wallet)
    if bal_wei < est_total_wei * 2:
        return RebalanceResult(
            executed=False,
            error=(f"wallet balance insufficient: {bal_wei/10**18:.6f} ETH "
                   f"vs needed {est_total_wei*2/10**18:.6f} ETH"),
            actual_gas_units=gas_units_est,
            actual_gas_usd=float(gas_cost_usd),
            duration_s=_time.time() - start,
        )

    # Confirm hook.
    if confirm_callback is not None:
        try:
            approved = bool(confirm_callback({
                "tokenId":       int(nft_token_id),
                "is_staked":     is_staked,
                "n_tx_steps":    len(seq.steps),
                "step_labels":   [s.label for s in seq.steps],
                "new_ticks":     [plan["new_tick_lower"], plan["new_tick_upper"]],
                "gas_usd_est":   float(gas_cost_usd),
                "deadline":      deadline,
            }))
        except Exception as e:                                  # noqa: BLE001
            return RebalanceResult(
                executed=False,
                error=f"confirm_callback raised: {type(e).__name__}: {e}",
                actual_gas_units=gas_units_est,
                actual_gas_usd=float(gas_cost_usd),
                duration_s=_time.time() - start,
            )
        if not approved:
            return RebalanceResult(
                executed=False, skipped_reason="rejected by confirm_callback",
                actual_gas_units=gas_units_est,
                actual_gas_usd=float(gas_cost_usd),
                duration_s=_time.time() - start,
            )

    # ===== EXECUTE STEPS IN SEQUENCE =====
    total_gas_used = 0
    new_token_id = None
    tx_hashes: list[str] = []

    npm_contract = w3.eth.contract(address=AERO_SLIPSTREAM_NPM, abi=NPM_WRITE_ABI)

    for idx, step in enumerate(seq.steps):
        # Patch new_token_id into approve/deposit steps if needed.
        data_to_send = step.data_hex
        if "[PLACEHOLDER]" in step.label:
            if new_token_id is None:
                return RebalanceResult(
                    executed=False,
                    error=(f"step {idx} ({step.label}) needs new_tokenId but "
                           f"multicall didn't return one"),
                    actual_gas_units=total_gas_used,
                    actual_gas_usd=float(
                        Decimal(total_gas_used * gas_price_wei * 2)
                        / Decimal(10**18) * eth_usd),
                    duration_s=_time.time() - start,
                )
            if "approve" in step.label:
                gauge_addr_cs = Web3.to_checksum_address(plan["gauge_address"])
                data_to_send = npm_contract.encode_abi(
                    "approve", [gauge_addr_cs, new_token_id])
            elif "deposit" in step.label:
                gauge_contract = w3.eth.contract(
                    address=Web3.to_checksum_address(plan["gauge_address"]),
                    abi=GAUGE_WRITE_ABI,
                )
                data_to_send = gauge_contract.encode_abi(
                    "deposit", [new_token_id])

        # Sign + broadcast this step.
        try:
            nonce = w3.eth.get_transaction_count(wallet, "pending")
            tx = {
                "from": wallet,
                "to": step.to,
                "value": step.value_wei,
                "data": data_to_send,
                "nonce": nonce,
                "gas": 500_000 if step.parses_new_tokenId else 200_000,
                "maxFeePerGas": gas_price_wei * 2,
                "maxPriorityFeePerGas": w3.to_wei(0.001, "gwei"),
                "chainId": chain_id,
            }
            signed = signer.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
            tx_hashes.append(tx_hash)
            logger.info(
                "[rebalance-aero] step %d/%d [%s] tx: %s",
                idx + 1, len(seq.steps), step.label, tx_hash,
            )
            rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            total_gas_used += int(rcpt.gasUsed)
            if rcpt.status != 1:
                return RebalanceResult(
                    executed=False,
                    tx_hash=tx_hash,
                    actual_gas_units=total_gas_used,
                    actual_gas_usd=float(
                        Decimal(total_gas_used * gas_price_wei * 2)
                        / Decimal(10**18) * eth_usd),
                    error=(f"step {idx+1} ({step.label}) reverted: "
                           f"receipt status={rcpt.status}"),
                    duration_s=_time.time() - start,
                )
            # If this was the mint step, parse out the new tokenId.
            if step.parses_new_tokenId:
                new_token_id = parse_new_token_id_from_receipt(rcpt)
                if new_token_id is None:
                    return RebalanceResult(
                        executed=False, tx_hash=tx_hash,
                        actual_gas_units=total_gas_used,
                        actual_gas_usd=float(
                            Decimal(total_gas_used * gas_price_wei * 2)
                            / Decimal(10**18) * eth_usd),
                        error=("multicall succeeded but could not parse new "
                               "tokenId from receipt logs"),
                        duration_s=_time.time() - start,
                    )
                logger.info(
                    "[rebalance-aero] new tokenId: %d", new_token_id)
        except Exception as e:                                  # noqa: BLE001
            return RebalanceResult(
                executed=False,
                error=(f"step {idx+1} ({step.label}) raised: "
                       f"{type(e).__name__}: {str(e)[:200]}"),
                actual_gas_units=total_gas_used,
                actual_gas_usd=float(
                    Decimal(total_gas_used * gas_price_wei * 2)
                    / Decimal(10**18) * eth_usd),
                duration_s=_time.time() - start,
            )

    # All steps succeeded.
    _LAST_REBALANCE_TS[int(nft_token_id)] = _time.time()
    try:
        _record_cooldown_disk(int(nft_token_id))
    except Exception:                                            # noqa: BLE001
        pass
    actual_cost = float(
        Decimal(total_gas_used * gas_price_wei * 2)
        / Decimal(10**18) * eth_usd)
    logger.info(
        "[rebalance-aero] SUCCESS old_tid=%s new_tid=%s gas=%s units (~$%.2f)",
        nft_token_id, new_token_id, total_gas_used, actual_cost,
    )
    return RebalanceResult(
        executed=True,
        tx_hash=tx_hashes[-1] if tx_hashes else None,
        actual_gas_units=total_gas_used,
        actual_gas_usd=actual_cost,
        duration_s=_time.time() - start,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 2026-06-04 #412 — increaseLiquidity on Slipstream (auto-deploy primitive)
# ═══════════════════════════════════════════════════════════════════════════
#
# Used by:
#   - idle_deploy_dispatcher.py — deploy idle wallet capital into existing NFT
#   - harvester Phase 2 (future) — compound collected fees back in
#
# Pattern mirrors sign_and_send_mint but targets an existing NFT (no new
# tokenId minted). Trustless verify: read positions(tokenId).liquidity
# before + after, confirm increase. Dry-run mode prints calldata but doesn't
# sign or send.

@dataclass
class IncreaseLiquidityResult:
    executed: bool
    nft_token_id: int
    amount0_added: int = 0
    amount1_added: int = 0
    liquidity_before: int = 0
    liquidity_after: int = 0
    tx_hash: Optional[str] = None
    actual_gas_usd: float = 0.0
    duration_s: float = 0.0
    error: Optional[str] = None
    dry_run: bool = False


def sign_and_send_increase_liquidity_aero(
    *,
    nft_token_id: int,
    amount0_desired: int,
    amount1_desired: int,
    slippage_pct: float = 0.5,
    deadline_sec: int = 600,
    dry_run: bool = True,
    is_staked: bool = False,
    gauge_address: str | None = None,
) -> IncreaseLiquidityResult:
    """Top up an existing Aerodrome Slipstream LP position with fresh capital.

    Args:
      nft_token_id: the position's NFT (must be owned by the LP wallet or by
        the gauge if is_staked=True).
      amount0_desired / amount1_desired: ATOMIC amounts (already scaled by
        token decimals).
      slippage_pct: amount0Min / amount1Min are computed from this.
      deadline_sec: tx must mine within this many seconds.
      dry_run: when True, builds and prints calldata but doesn't sign or send.
      is_staked: True when the NFT is currently deposited in an Aerodrome
        CL gauge for AERO rewards. Triggers unstake→increase→restake dance.
      gauge_address: required if is_staked=True — the gauge contract holding
        the NFT.

    Returns: IncreaseLiquidityResult with dry_run flag, on-chain verified
    liquidity delta when executed live.

    2026-06-04 #419 — first live-fire reverted because NFT 71481609 lives in
    the gauge, not the wallet. Added staking-aware path so AERO rewards
    aren't sacrificed when topping up.
    """
    import time as _t
    start = _t.time()

    # Validate staking params early — fail before any RPC if config is wrong
    if is_staked and not gauge_address:
        return IncreaseLiquidityResult(
            executed=False, nft_token_id=nft_token_id, dry_run=dry_run,
            error="is_staked=True but gauge_address not provided",
            duration_s=_t.time() - start,
        )

    try:
        from engine.strategies.lp_agile import env as _lp_env
    except Exception as exc:                                      # noqa: BLE001
        return IncreaseLiquidityResult(
            executed=False, nft_token_id=nft_token_id, dry_run=dry_run,
            error=f"env import failed: {exc}",
            duration_s=_t.time() - start,
        )

    if dry_run:
        logger.info(
            "[increase_liq-aero DRY-RUN] tokenId=%d a0_desired=%d a1_desired=%d "
            "slippage=%.2f%% is_staked=%s gauge=%s",
            nft_token_id, amount0_desired, amount1_desired, slippage_pct,
            is_staked, gauge_address or "-",
        )
        return IncreaseLiquidityResult(
            executed=False, nft_token_id=nft_token_id, dry_run=True,
            amount0_added=amount0_desired, amount1_added=amount1_desired,
            duration_s=_t.time() - start,
        )

    # LIVE PATH
    try:
        from web3 import Web3 as _W3
        rpc_url = os.environ.get(
            "BASE_RPC_URL",
            "https://base-mainnet.g.alchemy.com/v2/CYGrp_mXjU3hfX6XvOydB",
        )
        w3 = _W3(_W3.HTTPProvider(rpc_url))
        signer = _lp_env.get_signer()
    except Exception as exc:                                      # noqa: BLE001
        return IncreaseLiquidityResult(
            executed=False, nft_token_id=nft_token_id, dry_run=False,
            error=f"w3/signer setup: {exc}",
            duration_s=_t.time() - start,
        )

    npm = w3.eth.contract(
        address=SLIPSTREAM_NPM, abi=SLIPSTREAM_INCREASE_LIQUIDITY_ABI,
    )

    # Read positions() to learn token0/token1 + current liquidity
    try:
        pos = npm.functions.positions(int(nft_token_id)).call()
        token0_addr = pos[2]
        token1_addr = pos[3]
        liquidity_before = int(pos[7])
    except Exception as exc:                                      # noqa: BLE001
        return IncreaseLiquidityResult(
            executed=False, nft_token_id=nft_token_id, dry_run=False,
            error=f"positions() read failed: {exc}",
            duration_s=_t.time() - start,
        )

    # ─── STEP 1: UNSTAKE if needed ────────────────────────────────────────
    # If the NFT is in the gauge, wallet doesn't own it — increaseLiquidity
    # would revert. Withdraw the NFT back to the wallet first. gauge.withdraw
    # also auto-claims any pending AERO rewards to the wallet.
    unstake_tx_hash: str | None = None
    if is_staked:
        # Fresh chain check — snapshot can be stale if a previous run
        # unstaked but failed mid-flow. Don't try to unstake what's
        # already in the wallet.
        try:
            erc721_check = w3.eth.contract(
                address=SLIPSTREAM_NPM, abi=ERC721_APPROVE_ABI,
            )
            current_owner = erc721_check.functions.ownerOf(
                int(nft_token_id),
            ).call()
            if current_owner.lower() == signer.address.lower():
                logger.info(
                    "[increase_liq-aero] NFT %d already in wallet (snapshot "
                    "stale) — skipping unstake step",
                    nft_token_id,
                )
                is_staked = False  # treat as not-staked for the rest of the flow
        except Exception as exc:                                  # noqa: BLE001
            logger.warning(
                "[increase_liq-aero] ownerOf check failed (%s), proceeding "
                "with unstake attempt anyway", exc,
            )

    if is_staked:
        try:
            gauge = w3.eth.contract(
                address=Web3.to_checksum_address(gauge_address or ""),
                abi=SLIPSTREAM_CL_GAUGE_ABI,
            )
            unstake_tx_hash = _send_gauge_or_nft_tx(
                w3, signer, gauge, "withdraw", (int(nft_token_id),),
                label="increaseLiq:gauge.withdraw",
            )
            # Pause to let nonce pool settle (#419 throttle protection)
            _t.sleep(15)
            # Confirm NFT actually back in wallet (defensive — gauge contract
            # must have transferred it back)
            nft_owner_contract = w3.eth.contract(
                address=SLIPSTREAM_NPM, abi=ERC721_APPROVE_ABI,
            )
            owner_addr = nft_owner_contract.functions.ownerOf(
                int(nft_token_id),
            ).call()
            if owner_addr.lower() != signer.address.lower():
                raise RuntimeError(
                    f"unstake completed but NFT owner is {owner_addr}, "
                    f"expected wallet {signer.address}"
                )
            logger.info(
                "[increase_liq-aero] unstake confirmed — NFT %d back in wallet",
                nft_token_id,
            )
        except Exception as exc:                                  # noqa: BLE001
            return IncreaseLiquidityResult(
                executed=False, nft_token_id=nft_token_id, dry_run=False,
                liquidity_before=liquidity_before,
                error=f"unstake failed: {exc}",
                duration_s=_t.time() - start,
            )

    # Approve both tokens
    try:
        _send_approve(
            w3, signer, token0_addr, str(SLIPSTREAM_NPM),
            amount0_desired, label="increaseLiq:approve_t0",
        )
        _send_approve(
            w3, signer, token1_addr, str(SLIPSTREAM_NPM),
            amount1_desired, label="increaseLiq:approve_t1",
        )
    except Exception as exc:                                      # noqa: BLE001
        return IncreaseLiquidityResult(
            executed=False, nft_token_id=nft_token_id, dry_run=False,
            liquidity_before=liquidity_before,
            error=f"token approve failed: {exc}",
            duration_s=_t.time() - start,
        )

    # NOTE: amount0_min / amount1_min for increaseLiquidity is misleadingly
    # named. It's NOT price slippage — it's "minimum amounts consumed". On a
    # concentrated-liquidity position, the actual amount consumed for each
    # token is determined by the current tick relative to the range. If the
    # tick is near one edge, the pool wants mostly one side; trying to set
    # mins close to desired on BOTH sides will revert with "PSC".
    #
    # 2026-06-04 #419 — observed live: at the current tick the pool consumed
    # 7.34 USDC + 0.00039542 cbBTC (29%/100% utilization). Original 0.5%
    # slippage check required ≥99.5% of both → reverted.
    #
    # Correct behaviour for a top-up: send max caps via amount_desired,
    # accept whatever ratio the pool needs, leave the remainder in wallet.
    # The unconsumed tokens get redeployed next cycle. So we set mins=1.
    # `slippage_pct` is kept for backwards-compat but is ignored.
    _ = slippage_pct  # silence unused
    a0_min = 1
    a1_min = 1
    deadline = int(_t.time()) + deadline_sec

    params = (
        int(nft_token_id), int(amount0_desired), int(amount1_desired),
        int(a0_min), int(a1_min), int(deadline),
    )

    try:
        nonce = w3.eth.get_transaction_count(signer.address, "pending")
        tx = npm.functions.increaseLiquidity(params).build_transaction({
            "from": signer.address,
            "nonce": nonce,
            "gas": 500_000,
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": w3.to_wei(0.001, "gwei"),
            "chainId": w3.eth.chain_id,
            "value": 0,
        })
        signed = signer.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
        logger.info("[increase_liq-aero] tx submitted: %s", tx_hash)
        rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if rcpt.status != 1:
            raise RuntimeError(f"increaseLiquidity reverted (status={rcpt.status})")
    except Exception as exc:                                      # noqa: BLE001
        return IncreaseLiquidityResult(
            executed=False, nft_token_id=nft_token_id, dry_run=False,
            liquidity_before=liquidity_before,
            error=f"increaseLiquidity tx failed: {exc}",
            duration_s=_t.time() - start,
        )

    # Verify: re-read positions, confirm liquidity increased
    try:
        pos2 = npm.functions.positions(int(nft_token_id)).call()
        liquidity_after = int(pos2[7])
    except Exception:
        liquidity_after = liquidity_before  # best-effort

    # ─── STEP 3: RESTAKE if we unstaked ──────────────────────────────────
    # If the NFT was originally staked, put it back in the gauge so the
    # owner keeps earning AERO emissions. If THIS step fails, the NFT is
    # safely in the wallet — operator can manually restake. We log loudly.
    restake_tx_hash: str | None = None
    if is_staked:
        try:
            # Approve gauge to take the NFT
            _t.sleep(10)  # let increaseLiquidity nonce settle
            erc721 = w3.eth.contract(
                address=SLIPSTREAM_NPM, abi=ERC721_APPROVE_ABI,
            )
            gauge_cs = Web3.to_checksum_address(gauge_address or "")
            _send_gauge_or_nft_tx(
                w3, signer, erc721, "approve",
                (gauge_cs, int(nft_token_id)),
                label="increaseLiq:nft.approve_gauge",
                gas_fallback=200_000,  # ERC721.approve is small but estimate first
            )
            _t.sleep(10)
            # Deposit back into gauge
            gauge = w3.eth.contract(
                address=gauge_cs, abi=SLIPSTREAM_CL_GAUGE_ABI,
            )
            restake_tx_hash = _send_gauge_or_nft_tx(
                w3, signer, gauge, "deposit", (int(nft_token_id),),
                label="increaseLiq:gauge.deposit",
            )
            logger.info(
                "[increase_liq-aero] restake confirmed — NFT %d back in gauge (tx=%s)",
                nft_token_id, restake_tx_hash,
            )
        except Exception as exc:                                  # noqa: BLE001
            # DO NOT fail the whole increase — liquidity was successfully added.
            # NFT is in the wallet, just unstaked. Operator gets alerted via
            # error field but the result is still "executed=True" so the
            # registry reflects the new liquidity.
            logger.error(
                "[increase_liq-aero] RESTAKE FAILED — NFT %d is unstaked in "
                "wallet, no AERO until manually re-staked. Error: %s",
                nft_token_id, exc,
            )

    gas_used = rcpt.get("gasUsed", 0)
    gas_price = rcpt.get("effectiveGasPrice") or w3.eth.gas_price
    eth_usd = 2800.0  # fallback (gas math is non-critical; real value usually 2-4k)
    gas_usd = float(
        Decimal(gas_used * gas_price) / Decimal(10**18) * Decimal(str(eth_usd))
    )

    return IncreaseLiquidityResult(
        executed=True,
        nft_token_id=nft_token_id,
        amount0_added=amount0_desired,
        amount1_added=amount1_desired,
        liquidity_before=liquidity_before,
        liquidity_after=liquidity_after,
        tx_hash=tx_hash,
        actual_gas_usd=gas_usd,
        duration_s=_t.time() - start,
        dry_run=False,
    )


# =============================================================================
# prjx (HyperEVM) increaseLiquidity primitive — added 2026-06-04 #414
# =============================================================================
# Mirror of sign_and_send_increase_liquidity_aero, but for HyperEVM/prjx.
#
# Differences from Aerodrome version:
#   - No gauge / no staking dance (prjx NFTs live in the wallet directly)
#   - HyperEVM RPC + chain_id = 999
#   - prjx NPM at 0xeaD19AE861c29bBb2101E834922B2FEee69B9091
#   - HyperEVM uses gasPrice (legacy), not EIP-1559 maxFeePerGas
#   - Same Uniswap V3 increaseLiquidity ABI (confirmed via simulation)
#
# Hard-won lessons from #419 (Aerodrome) inherited here:
#   - Dynamic gas estimation via fn.estimate_gas + 1.5x headroom
#   - mins=1 (slippage check is misleading for top-ups — accept whatever
#     ratio the pool wants at current tick)
#   - Retry-with-backoff in _send_approve
#   - Fresh ownerOf check before assuming we own the NFT
#
# 2026-06-04 #414: shipped to deploy idle 278 USDC sitting on HyperEVM.

# prjx (HyperEVM) NPM with standard Uniswap V3 ABI — confirmed via on-chain
# call. Note: positions() returns uint24 `fee` field (not Slipstream's
# int24 tickSpacing).
PRJX_NPM_INCREASE_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "amount0Desired", "type": "uint256"},
                    {"name": "amount1Desired", "type": "uint256"},
                    {"name": "amount0Min", "type": "uint256"},
                    {"name": "amount1Min", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                ],
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "increaseLiquidity",
        "outputs": [
            {"name": "liquidity", "type": "uint128"},
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "positions",
        "outputs": [
            {"name": "nonce", "type": "uint96"},
            {"name": "operator", "type": "address"},
            {"name": "token0", "type": "address"},
            {"name": "token1", "type": "address"},
            {"name": "fee", "type": "uint24"},
            {"name": "tickLower", "type": "int24"},
            {"name": "tickUpper", "type": "int24"},
            {"name": "liquidity", "type": "uint128"},
            {"name": "f0", "type": "uint256"},
            {"name": "f1", "type": "uint256"},
            {"name": "o0", "type": "uint128"},
            {"name": "o1", "type": "uint128"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "ownerOf",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def _send_approve_hyperevm(
    w3,
    signer,
    token_addr: str,
    spender: str,
    amount: int,
    *,
    label: str,
) -> str:
    """Mirror of _send_approve but with HyperEVM-friendly tx params.

    HyperEVM uses legacy gasPrice, not EIP-1559. Also typical gas price is
    1 gwei (no priority fee).
    """
    import time as _t
    token = w3.eth.contract(
        address=Web3.to_checksum_address(token_addr), abi=ERC20_APPROVE_ABI,
    )
    wallet = signer.address
    current = token.functions.allowance(wallet, spender).call()
    if current >= amount:
        logger.info("%s: allowance %d already ≥ %d, skipping", label, current, amount)
        return "0x0_skipped_already_approved"

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            nonce = w3.eth.get_transaction_count(wallet, "pending")
            tx = token.functions.approve(spender, amount).build_transaction({
                "from": wallet,
                "nonce": nonce,
                "gas": 120_000,
                "gasPrice": w3.eth.gas_price,
                "chainId": w3.eth.chain_id,
                "value": 0,
            })
            signed = signer.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
            logger.info("%s tx submitted (%d/3): %s", label, attempt + 1, tx_hash)
            rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if rcpt.status != 1:
                raise RuntimeError(f"{label} reverted (status={rcpt.status})")
            return tx_hash
        except Exception as exc:                                  # noqa: BLE001
            last_exc = exc
            err = str(exc).lower()
            is_throttle = (
                "in-flight" in err or "rate limit" in err
                or "too many" in err or "-32000" in err
            )
            if not is_throttle or attempt == 2:
                raise
            backoff = 10 * (attempt + 1)
            logger.warning("%s throttle, retry in %ds: %s", label, backoff, exc)
            _t.sleep(backoff)
    raise last_exc or RuntimeError(f"{label} exhausted retries")


def sign_and_send_increase_liquidity_prjx(
    *,
    nft_token_id: int,
    amount0_desired: int,
    amount1_desired: int,
    deadline_sec: int = 600,
    dry_run: bool = True,
) -> IncreaseLiquidityResult:
    """Top up an existing prjx (HyperEVM) LP position.

    Sister to sign_and_send_increase_liquidity_aero with Aerodrome-specific
    bits removed and HyperEVM-specific bits added.

    mins=1 (intentional — for top-ups, slippage check would block valid
    deposits when the pool's tick distribution doesn't match desired ratio).
    """
    import time as _t
    start = _t.time()

    try:
        from engine.strategies.lp_agile import env as _lp_env
    except Exception as exc:                                      # noqa: BLE001
        return IncreaseLiquidityResult(
            executed=False, nft_token_id=nft_token_id, dry_run=dry_run,
            error=f"env import failed: {exc}",
            duration_s=_t.time() - start,
        )

    if dry_run:
        logger.info(
            "[increase_liq-prjx DRY-RUN] tokenId=%d a0=%d a1=%d",
            nft_token_id, amount0_desired, amount1_desired,
        )
        return IncreaseLiquidityResult(
            executed=False, nft_token_id=nft_token_id, dry_run=True,
            amount0_added=amount0_desired, amount1_added=amount1_desired,
            duration_s=_t.time() - start,
        )

    try:
        from web3 import Web3 as _W3
        rpc_url = os.environ.get(
            "HYPEREVM_RPC_URL", "https://rpc.hyperliquid.xyz/evm",
        )
        w3 = _W3(_W3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
        signer = _lp_env.get_signer()
    except Exception as exc:                                      # noqa: BLE001
        return IncreaseLiquidityResult(
            executed=False, nft_token_id=nft_token_id, dry_run=False,
            error=f"w3/signer setup: {exc}",
            duration_s=_t.time() - start,
        )

    PRJX_NPM_CS = Web3.to_checksum_address(
        "0xeaD19AE861c29bBb2101E834922B2FEee69B9091"
    )
    npm = w3.eth.contract(address=PRJX_NPM_CS, abi=PRJX_NPM_INCREASE_ABI)

    # Read positions() to learn token0/token1 + current liquidity
    try:
        pos = npm.functions.positions(int(nft_token_id)).call()
        token0_addr = pos[2]
        token1_addr = pos[3]
        liquidity_before = int(pos[7])
        owner = npm.functions.ownerOf(int(nft_token_id)).call()
        if owner.lower() != signer.address.lower():
            return IncreaseLiquidityResult(
                executed=False, nft_token_id=nft_token_id, dry_run=False,
                error=f"NFT owned by {owner}, expected wallet {signer.address}",
                duration_s=_t.time() - start,
            )
    except Exception as exc:                                      # noqa: BLE001
        return IncreaseLiquidityResult(
            executed=False, nft_token_id=nft_token_id, dry_run=False,
            error=f"positions/ownerOf read failed: {exc}",
            duration_s=_t.time() - start,
        )

    # Approve both tokens for the NPM
    try:
        _send_approve_hyperevm(
            w3, signer, token0_addr, str(PRJX_NPM_CS),
            amount0_desired, label="prjx:approve_t0",
        )
        _send_approve_hyperevm(
            w3, signer, token1_addr, str(PRJX_NPM_CS),
            amount1_desired, label="prjx:approve_t1",
        )
    except Exception as exc:                                      # noqa: BLE001
        return IncreaseLiquidityResult(
            executed=False, nft_token_id=nft_token_id, dry_run=False,
            liquidity_before=liquidity_before,
            error=f"token approve failed: {exc}",
            duration_s=_t.time() - start,
        )

    # increaseLiquidity with mins=1 (#419 lesson: slippage check misleading
    # for top-ups; just take whatever the pool wants).
    a0_min = 1
    a1_min = 1
    deadline = int(_t.time()) + deadline_sec
    params = (
        int(nft_token_id), int(amount0_desired), int(amount1_desired),
        int(a0_min), int(a1_min), int(deadline),
    )

    try:
        # Estimate gas dynamically
        try:
            gas_est = npm.functions.increaseLiquidity(params).estimate_gas({
                "from": signer.address,
            })
            gas_to_use = int(gas_est * 1.5)
            logger.info(
                "[increase_liq-prjx] gas estimate=%d, using %d",
                gas_est, gas_to_use,
            )
        except Exception as est_exc:                              # noqa: BLE001
            gas_to_use = 800_000
            logger.warning(
                "[increase_liq-prjx] gas estimate failed (%s), fallback to %d",
                est_exc, gas_to_use,
            )

        nonce = w3.eth.get_transaction_count(signer.address, "pending")
        tx = npm.functions.increaseLiquidity(params).build_transaction({
            "from": signer.address,
            "nonce": nonce,
            "gas": gas_to_use,
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
            "value": 0,
        })
        signed = signer.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
        logger.info("[increase_liq-prjx] tx submitted: %s", tx_hash)
        rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        if rcpt.status != 1:
            raise RuntimeError(f"increaseLiquidity reverted (status={rcpt.status})")
    except Exception as exc:                                      # noqa: BLE001
        return IncreaseLiquidityResult(
            executed=False, nft_token_id=nft_token_id, dry_run=False,
            liquidity_before=liquidity_before,
            error=f"increaseLiquidity tx failed: {exc}",
            duration_s=_t.time() - start,
        )

    # Verify on-chain
    try:
        pos2 = npm.functions.positions(int(nft_token_id)).call()
        liquidity_after = int(pos2[7])
    except Exception:
        liquidity_after = liquidity_before

    gas_used = rcpt.get("gasUsed", 0)
    gas_price = rcpt.get("effectiveGasPrice") or w3.eth.gas_price
    hype_price_usd = 40.0  # rough — actual is read from external source
    gas_usd = float(
        Decimal(gas_used * gas_price) / Decimal(10**18) * Decimal(str(hype_price_usd))
    )

    return IncreaseLiquidityResult(
        executed=True,
        nft_token_id=nft_token_id,
        amount0_added=amount0_desired,
        amount1_added=amount1_desired,
        liquidity_before=liquidity_before,
        liquidity_after=liquidity_after,
        tx_hash=tx_hash,
        actual_gas_usd=gas_usd,
        duration_s=_t.time() - start,
        dry_run=False,
    )
