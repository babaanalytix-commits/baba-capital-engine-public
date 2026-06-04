"""engine/strategies/lp_agile/wallet.py — read-only LP-NFT inspector.

For any EVM address, enumerate the wallet's Uniswap-V3-style LP positions
on a given protocol (PRJX / Uniswap V3 / Slipstream). Returns range bounds,
liquidity, in/out-of-range status, accrued fees, and current $ value.

**Read-only.** This module NEVER reads or writes a private key. It only
queries view functions on the NonfungiblePositionManager + pool contracts.
Phase 2 auto-execute will live in a separate `lp_executor.py` that loads the
DEDICATED LP wallet key per [[feedback-lp-dedicated-wallet]].

Usage:
    from engine.data.lp_pools import get_adapter
    from engine.strategies.lp_agile.types import Protocol
    from engine.strategies.lp_agile.wallet import read_lp_nft_positions

    adapter = get_adapter(Protocol.PRJX)
    positions = read_lp_nft_positions(adapter, "0xYourLPWallet...")
    for p in positions:
        print(p.summary_line())
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from web3 import Web3

from engine.data.lp_pools._abis import (
    ERC20_ABI, UNIV3_POOL_ABI, UNIV3_POSITION_MANAGER_ABI,
)
from engine.data.lp_pools._base import PoolDataAdapter
from engine.data.lp_pools._evm import (
    get_w3, safe_call, sqrt_price_x96_to_price, tick_to_price,
)

logger = logging.getLogger("engine.strategies.lp_agile.wallet")


# ---------------------------------------------------------------------------
# Live uncollected-fee reads (the prjx $0 fix, 2026-06-01)
# ---------------------------------------------------------------------------
# Minimal pool ABIs: feeGrowthGlobal{0,1}X128 + ticks(). UniV3 and Slipstream
# differ in the ticks() struct (Slipstream inserts stakedLiquidityNet +
# rewardGrowthOutside), so feeGrowthOutside lands at different indices.
_FEE_GROWTH_GLOBAL_ABI = [
    {"inputs": [], "name": "feeGrowthGlobal0X128",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "feeGrowthGlobal1X128",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
]
_UNIV3_TICKS_ABI = [{"inputs": [{"name": "tick", "type": "int24"}], "name": "ticks", "outputs": [
    {"name": "liquidityGross", "type": "uint128"}, {"name": "liquidityNet", "type": "int128"},
    {"name": "feeGrowthOutside0X128", "type": "uint256"}, {"name": "feeGrowthOutside1X128", "type": "uint256"},
    {"name": "tickCumulativeOutside", "type": "int56"}, {"name": "secondsPerLiquidityOutsideX128", "type": "uint160"},
    {"name": "secondsOutside", "type": "uint32"}, {"name": "initialized", "type": "bool"}],
    "stateMutability": "view", "type": "function"}]
_SLIPSTREAM_TICKS_ABI = [{"inputs": [{"name": "tick", "type": "int24"}], "name": "ticks", "outputs": [
    {"name": "liquidityGross", "type": "uint128"}, {"name": "liquidityNet", "type": "int128"},
    {"name": "stakedLiquidityNet", "type": "int128"},
    {"name": "feeGrowthOutside0X128", "type": "uint256"}, {"name": "feeGrowthOutside1X128", "type": "uint256"},
    {"name": "rewardGrowthOutsideX128", "type": "uint256"},
    {"name": "tickCumulativeOutside", "type": "int56"}, {"name": "secondsPerLiquidityOutsideX128", "type": "uint160"},
    {"name": "secondsOutside", "type": "uint32"}, {"name": "initialized", "type": "bool"}],
    "stateMutability": "view", "type": "function"}]


def _live_uncollected_fees(w3, pool_addr, *, is_slipstream, tick_lower, tick_upper,
                           current_tick, liquidity, tokens_owed0, tokens_owed1,
                           fg_inside0_last, fg_inside1_last):
    """Live uncollected fees (raw token0/1) from feeGrowth. tokensOwed alone is
    only the last checkpoint, so un-poked positions read $0 — this recovers the
    true accrual. Returns (fees0_raw, fees1_raw), or None on any read failure so
    the caller safely keeps the checkpointed tokensOwed (no regression)."""
    if int(liquidity) == 0:
        return int(tokens_owed0), int(tokens_owed1)
    try:
        from engine.data.lp_pools import _fees as _F
        fee0_idx, fee1_idx = (3, 4) if is_slipstream else (2, 3)
        ticks_abi = _SLIPSTREAM_TICKS_ABI if is_slipstream else _UNIV3_TICKS_ABI
        pool_fg = w3.eth.contract(address=Web3.to_checksum_address(pool_addr),
                                  abi=_FEE_GROWTH_GLOBAL_ABI + ticks_abi)
        g0 = pool_fg.functions.feeGrowthGlobal0X128().call()
        g1 = pool_fg.functions.feeGrowthGlobal1X128().call()
        lo = pool_fg.functions.ticks(int(tick_lower)).call()
        up = pool_fg.functions.ticks(int(tick_upper)).call()
        return _F.uncollected_both(
            liquidity=int(liquidity), tokens_owed0=int(tokens_owed0), tokens_owed1=int(tokens_owed1),
            fee_growth_global0_x128=int(g0), fee_growth_global1_x128=int(g1),
            lower_fee_growth_outside0_x128=int(lo[fee0_idx]), lower_fee_growth_outside1_x128=int(lo[fee1_idx]),
            upper_fee_growth_outside0_x128=int(up[fee0_idx]), upper_fee_growth_outside1_x128=int(up[fee1_idx]),
            fee_growth_inside0_last_x128=int(fg_inside0_last), fee_growth_inside1_last_x128=int(fg_inside1_last),
            tick_current=int(current_tick), tick_lower=int(tick_lower), tick_upper=int(tick_upper))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[wallet] live uncollected-fee read failed for pool %s (%s) — "
                       "falling back to checkpointed tokensOwed", pool_addr, exc)
        return None


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LPNFTPosition:
    """A single Uniswap-V3-style LP NFT position held by a wallet."""
    # Identity
    protocol: str                     # "prjx" | "uniswap_v3" | "slipstream"
    chain: str                        # "hyperevm" | "ethereum" | "base"
    nft_contract: str                 # NonfungiblePositionManager address
    token_id: int                     # the NFT id

    # Pool
    pool_address: str
    token0_symbol: str
    token1_symbol: str
    token0_address: str
    token1_address: str
    token0_decimals: int
    token1_decimals: int
    fee_bps: int

    # Range bounds
    tick_lower: int
    tick_upper: int
    price_lower: Decimal              # token1 per token0 (human units)
    price_upper: Decimal

    # Current pool state at time of read
    pool_tick: int
    pool_sqrt_price_x96: int
    pool_price: Decimal               # token1 per token0
    in_range: bool

    # Liquidity
    liquidity: int                    # raw Uniswap V3 liquidity unit
    amount0_human: Decimal            # current position composition
    amount1_human: Decimal

    # Fees owed (currently uncollected — last on-chain checkpoint)
    fees_owed0_human: Decimal
    fees_owed1_human: Decimal

    # USD valuation (best-effort; quote leg priced at $1 if stable)
    position_value_usd: Optional[Decimal]
    fees_owed_value_usd: Optional[Decimal]

    # Staking metadata (populated by wallet_staked.read_staked_positions).
    # Default False/None means "wallet-held, not staked" — back-compat with
    # all existing consumers that don't care about staking.
    staked: bool = False
    staked_in_gauge: Optional[str] = None       # gauge address (custodian)
    pending_aero: Optional[Decimal] = None      # claimable AERO (human units)
    pending_aero_usd: Optional[Decimal] = None  # USD value at current AERO price

    # Tagging
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def summary_line(self) -> str:
        """One-line human summary."""
        status = "🟢 IN RANGE" if self.in_range else "🔴 OUT OF RANGE"
        val = (f"${self.position_value_usd:,.2f}"
               if self.position_value_usd else "?")
        fees = (f"${self.fees_owed_value_usd:,.4f}"
                if self.fees_owed_value_usd else "?")
        stake_tag = ""
        if self.staked:
            aero = (f"  AERO+{self.pending_aero:.4f}"
                    if self.pending_aero else "")
            stake_tag = f"  ⚡STAKED{aero}"
        return (
            f"NFT #{self.token_id}  {self.protocol}:{self.token0_symbol}/{self.token1_symbol} "
            f"@{self.fee_bps}bps  {status}  "
            f"value={val}  fees_owed={fees}{stake_tag}  "
            f"range=[{self.price_lower:,.4f} → {self.price_upper:,.4f}]"
        )


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


# Common stable symbols — used for USD valuation fallback when on-chain
# stables-list (per-adapter) is unavailable.
STABLE_SYMBOLS = {"USDC", "USDT", "USDT0", "USD₮0", "USDC.E", "DAI",
                  "USDHL", "USDXL", "FEUSD", "USDE", "USDS"}

# 2026-05-31 (#PWA canonical-source-of-truth): treat BTC proxies as a
# priceable leg too. Without this, USDC/cbBTC pools fall through to the
# CoinGecko fallback (which can 429) and APR shows blank in the PWA.
# When the OTHER leg is a stable, the pair is fully priceable from
# pool_price alone. _usd_value() handles both orderings via the BTC_PROXY
# constant below.
BTC_PROXY_SYMBOLS = {"CBBTC", "WBTC", "BTC", "TBTC"}


def read_lp_nft_positions(
    adapter: PoolDataAdapter, wallet_address: str,
    *, max_positions: int = 200,
) -> list[LPNFTPosition]:
    """Enumerate every LP NFT held by `wallet_address` for this adapter's protocol.

    Returns empty list if the wallet holds none (or NPM call fails).

    Cost: 1 + N×~6 eth_calls (N = # NFTs). With HyperEVM public-RPC rate limit
    of 100 req/min, this works fine for typical subscriber wallets (<10 NFTs).
    """
    wallet = Web3.to_checksum_address(wallet_address)
    chain = adapter.chain
    w3 = get_w3(chain)

    npm = w3.eth.contract(
        address=Web3.to_checksum_address(adapter.position_manager_address),
        abi=UNIV3_POSITION_MANAGER_ABI,
    )

    nft_count = safe_call(
        lambda: npm.functions.balanceOf(wallet).call(),
        default=0,
        ctx=f"npm.balanceOf({wallet})",
    )
    if not nft_count:
        return []

    if nft_count > max_positions:
        logger.warning(
            "wallet %s holds %d NFTs > max_positions=%d — truncating",
            wallet, nft_count, max_positions,
        )
        nft_count = max_positions

    positions: list[LPNFTPosition] = []
    for i in range(nft_count):
        token_id = safe_call(
            lambda i=i: npm.functions.tokenOfOwnerByIndex(wallet, i).call(),
            default=None,
            ctx=f"npm.tokenOfOwnerByIndex({wallet},{i})",
        )
        if token_id is None:
            continue
        pos = _read_one_position(w3, adapter, npm, token_id)
        if pos is not None:
            positions.append(pos)

    return positions


def _read_one_position(
    w3, adapter: PoolDataAdapter, npm, token_id: int,
) -> Optional[LPNFTPosition]:
    """Read + decode one LP NFT.

    Handles both Uniswap-V3-style NPM (position[4]=fee, factory.getPool takes
    fee) and Slipstream-style NPM (position[4]=tickSpacing,
    factory.getPool takes tickSpacing). The two protocols return identical
    field layout positionally; only the SEMANTICS of slot[4] differ. We
    branch on the adapter to choose:
      - Factory.getPool signature: (t0,t1,uint24 fee) vs (t0,t1,int24 tickSpacing)
      - Pool ABI: UNIV3_POOL_ABI (7-field slot0) vs SLIPSTREAM_POOL_ABI (6-field)
      - fee_bps for the display dataclass: fee/100 vs pool.fee()/100
    """
    raw = safe_call(
        lambda: npm.functions.positions(token_id).call(),
        default=None,
        ctx=f"npm.positions({token_id})",
    )
    if raw is None:
        return None
    (_nonce, _operator, t0_addr, t1_addr, fee_or_spacing,
     tick_lower, tick_upper, liquidity,
     _feeGrowth0, _feeGrowth1, tokens_owed0, tokens_owed1) = raw

    # Skip zombie NFTs (closed positions where liquidity withdrawn but NFT
    # not burned). Common on Uniswap V3-style protocols. Without this guard,
    # PWA shows old closed positions as $0 ghosts. 2026-05-26 fix — caught
    # when Yomi's 4 closed PRJX NFTs surfaced after the NPM fix.
    if liquidity == 0 and tokens_owed0 == 0 and tokens_owed1 == 0:
        logger.debug("NFT %d has zero liquidity + zero fees owed — zombie, skipping",
                     token_id)
        return None

    is_slipstream = "aerodrome" in type(adapter).__name__.lower() \
        or "slipstream" in type(adapter).__name__.lower()

    # Resolve pool from factory. Slipstream's CLFactory.getPool takes
    # tickSpacing (int24); UniV3-style factory takes fee (uint24). We supply
    # the correct ABI for each.
    factory_addr = safe_call(
        lambda: w3.eth.contract(
            address=npm.address,
            abi=[{"inputs": [], "name": "factory",
                  "outputs": [{"name": "", "type": "address"}],
                  "stateMutability": "view", "type": "function"}],
        ).functions.factory().call(),
        default=None,
        ctx="npm.factory()",
    )
    if factory_addr is None:
        return None

    if is_slipstream:
        # Slipstream CLFactory: getPool(token0, token1, tickSpacing) → pool
        cl_factory_abi = [{
            "inputs": [{"name": "tokenA", "type": "address"},
                       {"name": "tokenB", "type": "address"},
                       {"name": "tickSpacing", "type": "int24"}],
            "name": "getPool",
            "outputs": [{"name": "", "type": "address"}],
            "stateMutability": "view", "type": "function",
        }]
        factory = w3.eth.contract(
            address=Web3.to_checksum_address(factory_addr),
            abi=cl_factory_abi,
        )
        pool_addr = safe_call(
            lambda: factory.functions.getPool(
                t0_addr, t1_addr, fee_or_spacing,
            ).call(),
            default=None,
            ctx=f"cl_factory.getPool({t0_addr},{t1_addr},tickSpacing={fee_or_spacing})",
        )
    else:
        from engine.data.lp_pools._abis import UNIV3_FACTORY_ABI
        factory = w3.eth.contract(
            address=Web3.to_checksum_address(factory_addr),
            abi=UNIV3_FACTORY_ABI,
        )
        pool_addr = safe_call(
            lambda: factory.functions.getPool(
                t0_addr, t1_addr, fee_or_spacing,
            ).call(),
            default=None,
            ctx=f"factory.getPool({t0_addr},{t1_addr},fee={fee_or_spacing})",
        )
    if not pool_addr or int(pool_addr, 16) == 0:
        logger.warning("NFT %d references pool that doesn't exist anymore", token_id)
        return None

    # Slot0 layout differs: Slipstream has 6 fields (no feeProtocol byte).
    if is_slipstream:
        from engine.data.lp_pools._abis import SLIPSTREAM_POOL_ABI as _POOL_ABI
    else:
        _POOL_ABI = UNIV3_POOL_ABI
    pool = w3.eth.contract(
        address=Web3.to_checksum_address(pool_addr),
        abi=_POOL_ABI,
    )
    slot0 = safe_call(lambda: pool.functions.slot0().call(), default=None,
                      ctx=f"pool.slot0() NFT {token_id}")
    if slot0 is None:
        return None
    sqrt_px96, current_tick = slot0[0], slot0[1]

    # Token metadata
    t0 = w3.eth.contract(address=t0_addr, abi=ERC20_ABI)
    t1 = w3.eth.contract(address=t1_addr, abi=ERC20_ABI)
    sym0 = safe_call(lambda: t0.functions.symbol().call(), default="?")
    sym1 = safe_call(lambda: t1.functions.symbol().call(), default="?")
    dec0 = safe_call(lambda: t0.functions.decimals().call(), default=18)
    dec1 = safe_call(lambda: t1.functions.decimals().call(), default=18)

    # Range bounds in price space
    price_lower = tick_to_price(tick_lower, dec0, dec1)
    price_upper = tick_to_price(tick_upper, dec0, dec1)
    pool_price  = sqrt_price_x96_to_price(sqrt_px96, dec0, dec1)
    in_range = tick_lower <= current_tick < tick_upper

    # Decompose liquidity into token amounts at current price (or at boundaries
    # if out of range). Standard Uniswap V3 math:
    amount0, amount1 = _liquidity_to_amounts(
        liquidity, sqrt_px96, tick_lower, tick_upper,
    )
    amount0_h = Decimal(amount0) / (Decimal(10) ** dec0)
    amount1_h = Decimal(amount1) / (Decimal(10) ** dec1)
    fees0_h = Decimal(tokens_owed0) / (Decimal(10) ** dec0)
    fees1_h = Decimal(tokens_owed1) / (Decimal(10) ** dec1)
    # 2026-06-01 (prjx $0 fix): tokensOwed is only the LAST checkpoint, so an
    # un-poked position reads $0. Derive the TRUE uncollected fees from feeGrowth.
    # Safe fallback to tokensOwed if any on-chain read fails.
    _live = _live_uncollected_fees(
        w3, pool_addr, is_slipstream=is_slipstream,
        tick_lower=tick_lower, tick_upper=tick_upper, current_tick=current_tick,
        liquidity=liquidity, tokens_owed0=tokens_owed0, tokens_owed1=tokens_owed1,
        fg_inside0_last=_feeGrowth0, fg_inside1_last=_feeGrowth1)
    if _live is not None:
        fees0_h = Decimal(_live[0]) / (Decimal(10) ** dec0)
        fees1_h = Decimal(_live[1]) / (Decimal(10) ** dec1)

    # USD valuation: if either leg is a stable, price the other in stables.
    pos_value_usd, fees_value_usd = _usd_value(
        sym0, sym1, amount0_h, amount1_h, fees0_h, fees1_h, pool_price,
    )

    # fee_bps for display:
    #   UniV3-style: fee_or_spacing is the fee in 100ths-of-bps (3000 → 30bps)
    #   Slipstream:  fee_or_spacing is tickSpacing; read pool.fee() (in pips,
    #                ÷100 for bps) per [[feedback_cbbtc_apr_data_bug_2026_05_25]].
    if is_slipstream:
        slip_fee_abi = [{"inputs": [], "name": "fee",
                         "outputs": [{"name": "", "type": "uint24"}],
                         "stateMutability": "view", "type": "function"}]
        fee_pool = w3.eth.contract(
            address=Web3.to_checksum_address(pool_addr), abi=slip_fee_abi,
        )
        fee_raw = safe_call(lambda: fee_pool.functions.fee().call(),
                            default=0, ctx=f"pool.fee() {pool_addr}")
        fee_bps_display = int(round(fee_raw / 100)) if fee_raw else 0
    else:
        fee_bps_display = int(fee_or_spacing // 100)

    return LPNFTPosition(
        protocol=adapter.chain.value if False else _protocol_name(adapter),
        chain=adapter.chain.value,
        nft_contract=str(npm.address),
        token_id=int(token_id),
        pool_address=Web3.to_checksum_address(pool_addr),
        token0_symbol=sym0,
        token1_symbol=sym1,
        token0_address=str(t0_addr),
        token1_address=str(t1_addr),
        token0_decimals=int(dec0),
        token1_decimals=int(dec1),
        fee_bps=fee_bps_display,
        tick_lower=int(tick_lower),
        tick_upper=int(tick_upper),
        price_lower=price_lower,
        price_upper=price_upper,
        pool_tick=int(current_tick),
        pool_sqrt_price_x96=int(sqrt_px96),
        pool_price=pool_price,
        in_range=in_range,
        liquidity=int(liquidity),
        amount0_human=amount0_h,
        amount1_human=amount1_h,
        fees_owed0_human=fees0_h,
        fees_owed1_human=fees1_h,
        position_value_usd=pos_value_usd,
        fees_owed_value_usd=fees_value_usd,
    )


# ---------------------------------------------------------------------------
# Uniswap V3 liquidity math
# ---------------------------------------------------------------------------


_Q96 = 2 ** 96


def _sqrt_ratio_at_tick(tick: int) -> int:
    """sqrt(1.0001^tick) × 2^96 — close-enough Python implementation.

    Production Uniswap uses a hand-tuned bit-twiddled exponent; we use
    float for the exponent then round to int. Precision is fine for ranges
    around mainstream tick magnitudes (|tick| < 887272).
    """
    return int(math.sqrt(1.0001 ** tick) * _Q96)


def _liquidity_to_amounts(
    liquidity: int, sqrt_px96: int, tick_lower: int, tick_upper: int,
) -> tuple[int, int]:
    """Decompose Uniswap V3 liquidity into (amount0_atomic, amount1_atomic)
    at the current price.

    Standard formulas: see Uniswap V3 whitepaper §6.2.
    """
    if liquidity == 0:
        return 0, 0
    sqrt_a = _sqrt_ratio_at_tick(tick_lower)
    sqrt_b = _sqrt_ratio_at_tick(tick_upper)
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    sqrt_c = max(sqrt_a, min(sqrt_b, sqrt_px96))

    # amount0 = L × (sqrt_b - sqrt_c) / (sqrt_c × sqrt_b / 2^96)
    if sqrt_c < sqrt_b:
        amount0 = (liquidity * (sqrt_b - sqrt_c) * _Q96) // (sqrt_c * sqrt_b)
    else:
        amount0 = 0
    # amount1 = L × (sqrt_c - sqrt_a) / 2^96
    if sqrt_c > sqrt_a:
        amount1 = (liquidity * (sqrt_c - sqrt_a)) // _Q96
    else:
        amount1 = 0
    return amount0, amount1


def _usd_value(
    sym0: str, sym1: str,
    amount0_h: Decimal, amount1_h: Decimal,
    fees0_h: Decimal, fees1_h: Decimal,
    pool_price: Decimal,
) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """Best-effort USD valuation using stable-leg detection.

    pool_price is token1 per token0 (human units). If sym1 is stable: USD-per-token0 = pool_price.
    If sym0 is stable: USD-per-token1 = 1/pool_price.
    Otherwise we can't price without an external oracle.
    """
    s0 = sym0.upper().lstrip("_").rstrip("0")
    s1 = sym1.upper().lstrip("_").rstrip("0")
    s0_is_stable = s0 in STABLE_SYMBOLS or "USD" in s0
    s1_is_stable = s1 in STABLE_SYMBOLS or "USD" in s1
    if s1_is_stable:
        usd_per_t0 = pool_price
        usd_per_t1 = Decimal(1)
    elif s0_is_stable:
        usd_per_t0 = Decimal(1)
        # Guard against pool_price == 0 — Yomi flagged 2026-05-31 that the
        # USDC/cbBTC card showed nothing for APR. Root cause traced to
        # silent None here when pool_price was momentarily 0/missing.
        if pool_price and pool_price > 0:
            usd_per_t1 = Decimal(1) / pool_price
        else:
            usd_per_t1 = Decimal(0)
    else:
        # Non-stable pair (e.g. HYPE/UBTC, cbBTC/WETH). Fall back to
        # CoinGecko prices via the lp_wallet_balance helper's cached lookup.
        # When CoinGecko is rate-limited (429) we PREFER partial pricing
        # over None — at least one leg's worth is better than dropping the
        # whole position from the PWA (Yomi flag 2026-05-31).
        try:
            from ops.wallet.lp_wallet_balance import _coingecko_price
            gecko_map = {
                "HYPE": "hyperliquid", "WHYPE": "hyperliquid",
                "BTC": "bitcoin", "UBTC": "coinbase-wrapped-btc",
                "WBTC": "wrapped-bitcoin", "CBBTC": "coinbase-wrapped-btc",
                "ETH": "ethereum", "UETH": "ethereum", "WETH": "weth",
                "AERO": "aerodrome-finance",
            }
            p0 = _coingecko_price(gecko_map.get(s0, ""))
            p1 = _coingecko_price(gecko_map.get(s1, ""))
            if p0 is not None and p1 is not None:
                usd_per_t0 = Decimal(str(p0))
                usd_per_t1 = Decimal(str(p1))
            elif p0 is not None and pool_price and pool_price > 0:
                # CoinGecko returned p0 only → derive p1 from pool_price
                usd_per_t0 = Decimal(str(p0))
                usd_per_t1 = Decimal(str(p0)) / pool_price
            elif p1 is not None and pool_price and pool_price > 0:
                # CoinGecko returned p1 only → derive p0 from pool_price
                usd_per_t1 = Decimal(str(p1))
                usd_per_t0 = Decimal(str(p1)) * pool_price
            else:
                return None, None
        except Exception:
            return None, None
    pos_val = amount0_h * usd_per_t0 + amount1_h * usd_per_t1
    fees_val = fees0_h * usd_per_t0 + fees1_h * usd_per_t1
    return pos_val, fees_val


def _protocol_name(adapter: PoolDataAdapter) -> str:
    """Derive short protocol slug from adapter's class."""
    name = type(adapter).__name__.lower()
    if "prjx" in name:
        return "prjx"
    if "uniswap" in name:
        return "uniswap_v3"
    if "aerodrome" in name or "slipstream" in name:
        return "slipstream"
    return name
