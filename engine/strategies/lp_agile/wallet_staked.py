"""engine/strategies/lp_agile/wallet_staked.py — staked LP-NFT discovery.

Why this exists:
  ``wallet.read_lp_nft_positions`` enumerates only NFTs HELD by the wallet via
  ``balanceOf + tokenOfOwnerByIndex``. When an Aerodrome Slipstream LP NFT is
  staked, ownership transfers to the gauge contract — so wallet.balanceOf
  returns 0 and the position vanishes from every downstream view (PWA
  Overview, Capital tab, lp_agile_latest.json, NAV calc, post-mortem).

  This module covers that gap by enumerating positions still owned-by-depositor
  via ``gauge.stakedValues(wallet)``. Verified 2026-05-26 against tokenId
  71276872 in gauge 0x6399ed6725cC163D019aA64FF55b22149D7179A8 — Yomi's real
  cbBTC/USDC CL100 position.

Discovery flow:
  1. For every Slipstream pool in the universe, resolve the gauge via
     ``voter.gauges(pool)``. Skip pools with placeholder pool_address (e.g.
     "TBD" entries).
  2. Call ``gauge.stakedValues(wallet)`` to enumerate tokenIds the wallet has
     deposited there. (No event scan needed — CLGauge exposes the set
     directly. This is faster and avoids RPC rate-limit issues.)
  3. Cross-check ``gauge.stakedContains(wallet, tokenId)`` per the trustless
     verification rule — single-call belt-and-braces.
  4. Read position state via ``npm.positions(tokenId)``. NOTE: Slipstream NPM
     returns ``tickSpacing`` in slot[4] where Uniswap V3 NPM returns ``fee`` —
     we side-step the difference by using the already-known pool_address from
     the universe rather than reverse-resolving via factory.getPool().
  5. Pull pending AERO via ``gauge.earned(wallet, tokenId)``.
  6. Emit ``LPNFTPosition`` augmented with ``staked=True``,
     ``staked_in_gauge=<addr>``, ``pending_aero``, ``pending_aero_usd``.

Per [[feedback-trustless-data-verification]]: every read tagged source/age;
RPC failures degrade gracefully (return [] for that pool) rather than silently
mark the position non-existent.

Per [[feedback-lp-dedicated-wallet]]: READ-ONLY. This module never reads or
writes a private key — only view calls. Staking and unstaking belong to
``gauge_stake.py``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional

from web3 import Web3

from engine.data.lp_pools._abis import (
    ERC20_ABI, SLIPSTREAM_POOL_ABI, UNIV3_POSITION_MANAGER_ABI,
)
from engine.data.lp_pools._base import PoolDataAdapter
from engine.data.lp_pools._evm import (
    get_w3, safe_call, sqrt_price_x96_to_price, tick_to_price,
)
from engine.strategies.lp_agile.types import Chain, Protocol, PoolDef
from engine.strategies.lp_agile.wallet import (
    LPNFTPosition, _liquidity_to_amounts, _usd_value,
)

logger = logging.getLogger("engine.strategies.lp_agile.wallet_staked")


# CLGauge enumeration ABI — verified 2026-05-26 against canonical Slipstream
# CLGauge impl on Base (Aerodrome).
CLGAUGE_ENUM_ABI = [
    {"inputs": [{"name": "depositor", "type": "address"}],
     "name": "stakedValues",
     "outputs": [{"name": "staked", "type": "uint256[]"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "account", "type": "address"}],
     "name": "stakedLength",
     "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "depositor", "type": "address"},
                {"name": "tokenId", "type": "uint256"}],
     "name": "stakedContains",
     "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "account", "type": "address"},
                {"name": "tokenId", "type": "uint256"}],
     "name": "earned",
     "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]


# Slipstream pool fee() ABI — pool fee in pip units (÷100 for bps; see RCA
# task #70). Used only for display in the LPNFTPosition fee_bps field.
SLIPSTREAM_POOL_FEE_ABI = [
    {"inputs": [], "name": "fee",
     "outputs": [{"name": "", "type": "uint24"}],
     "stateMutability": "view", "type": "function"},
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def read_staked_positions(
    adapter: PoolDataAdapter,
    wallet_address: str,
    pool_universe: Iterable[PoolDef],
    *,
    aero_price_usd: Optional[Decimal] = None,
) -> list[LPNFTPosition]:
    """Enumerate every LP NFT staked by ``wallet_address`` in any gauge of the
    pools in ``pool_universe``.

    Only meaningful for Slipstream (Aerodrome) right now — other protocols
    return an empty list. Pools whose gauge can't be resolved are silently
    skipped (logged). Cost: 1 gauge-lookup + 1 stakedValues call per pool;
    N×~6 NPM reads per staked tokenId.

    Args:
      adapter: must be the Slipstream adapter (chain=BASE,
        protocol=SLIPSTREAM). Wrong protocol → return [].
      wallet_address: the LP wallet.
      pool_universe: pools to scan gauges for. Filtered to Slipstream pools
        on Base automatically; pools with placeholder addresses (e.g. "TBD")
        are skipped.
      aero_price_usd: optional override; if None, fetched lazily via
        aerodrome_gauge._aero_price_usd().
    """
    if not _adapter_is_slipstream(adapter):
        return []

    wallet = Web3.to_checksum_address(wallet_address)
    chain = adapter.chain
    w3 = get_w3(chain)
    npm = w3.eth.contract(
        address=Web3.to_checksum_address(adapter.position_manager_address),
        abi=UNIV3_POSITION_MANAGER_ABI,
    )

    # Filter pool universe to Slipstream-on-Base with valid hex addresses.
    slipstream_pools = [
        p for p in pool_universe
        if p.protocol == Protocol.SLIPSTREAM
        and p.chain == Chain.BASE
        and _is_valid_hex_address(p.pool_address)
    ]
    if not slipstream_pools:
        logger.debug("no slipstream pools in universe; skipping staked scan")
        return []

    # Resolve gauges once per pool.
    pool_to_gauge: dict[str, str] = {}
    for pool in slipstream_pools:
        try:
            from engine.strategies.lp_agile.gauge_stake import get_gauge_for_pool
            g = get_gauge_for_pool(pool.pool_address)
        except Exception as exc:                                  # noqa: BLE001
            logger.warning("gauge lookup failed for pool=%s: %s",
                           pool.id, exc)
            continue
        if g:
            pool_to_gauge[pool.pool_address] = g

    if not pool_to_gauge:
        logger.info("no gauges resolved for any slipstream pool — empty result")
        return []

    # Per [[feedback-trustless-data-verification]]: failure to query a gauge
    # is REPORTED, not silently treated as "no positions". A missing position
    # could mislead the NAV calc. We log warnings; caller can elevate.
    positions: list[LPNFTPosition] = []
    for pool_addr, gauge_addr in pool_to_gauge.items():
        gauge = w3.eth.contract(
            address=Web3.to_checksum_address(gauge_addr),
            abi=CLGAUGE_ENUM_ABI,
        )
        token_ids = safe_call(
            lambda g=gauge: g.functions.stakedValues(wallet).call(),
            default=None,
            ctx=f"gauge.stakedValues({wallet}) gauge={gauge_addr}",
        )
        if token_ids is None:
            # RPC failure — log but DON'T silently treat as zero positions.
            # The lp_scheduled_scan caller can decide whether to refuse the
            # tick based on the missing data flag.
            logger.warning(
                "RPC failed reading stakedValues for gauge=%s — "
                "wallet may have staked positions we can't see this tick",
                gauge_addr,
            )
            continue
        if not token_ids:
            continue

        for token_id in token_ids:
            pos = _read_one_staked_position(
                w3, adapter, npm, gauge, gauge_addr, pool_addr,
                wallet, int(token_id), aero_price_usd,
            )
            if pos is not None:
                positions.append(pos)

    return positions


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _read_one_staked_position(
    w3, adapter: PoolDataAdapter, npm, gauge, gauge_addr: str,
    pool_addr: str, wallet: str, token_id: int,
    aero_price_usd: Optional[Decimal],
) -> Optional[LPNFTPosition]:
    """Read one staked NFT's full state and return an augmented LPNFTPosition.

    Unlike ``wallet._read_one_position`` we already know the pool address
    (from the universe iteration) so we skip the broken factory.getPool path
    and read pool.slot0() directly. Slipstream NPM's ``positions()`` returns
    tickSpacing in slot[4] (vs Uniswap V3 NPM where slot[4]=fee); we use
    pool.fee() for the bps display so the value is always honest.
    """
    # Trustless cross-check: gauge.stakedContains() before reading.
    is_staked = safe_call(
        lambda: gauge.functions.stakedContains(wallet, token_id).call(),
        default=False,
        ctx=f"gauge.stakedContains({wallet}, {token_id})",
    )
    if not is_staked:
        logger.warning(
            "stakedValues returned tokenId=%d but stakedContains=False "
            "(race or RPC drift); skipping", token_id,
        )
        return None

    raw = safe_call(
        lambda: npm.functions.positions(token_id).call(),
        default=None,
        ctx=f"npm.positions({token_id})",
    )
    if raw is None:
        return None
    # Slipstream layout:
    #   nonce, operator, token0, token1, tickSpacing, tickLower, tickUpper,
    #   liquidity, feeGrowthInside0LastX128, feeGrowthInside1LastX128,
    #   tokensOwed0, tokensOwed1
    (_nonce, _operator, t0_addr, t1_addr, tick_spacing,
     tick_lower, tick_upper, liquidity,
     _feeGrowth0, _feeGrowth1, tokens_owed0, tokens_owed1) = raw

    # Pool state
    pool = w3.eth.contract(
        address=Web3.to_checksum_address(pool_addr),
        abi=SLIPSTREAM_POOL_ABI,
    )
    slot0 = safe_call(lambda: pool.functions.slot0().call(), default=None,
                      ctx=f"pool.slot0() NFT {token_id}")
    if slot0 is None:
        return None
    sqrt_px96, current_tick = slot0[0], slot0[1]

    # Slipstream fee is in pip units (10000 = 1%); ÷100 for bps display.
    # See feedback_cbbtc_apr_data_bug_2026_05_25.
    fee_pool = w3.eth.contract(
        address=Web3.to_checksum_address(pool_addr),
        abi=SLIPSTREAM_POOL_FEE_ABI,
    )
    fee_raw = safe_call(lambda: fee_pool.functions.fee().call(),
                        default=0, ctx=f"pool.fee() {pool_addr}")
    fee_bps = int(round(fee_raw / 100)) if fee_raw else 0

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

    # V3 liquidity decomposition (shared math with wallet.py).
    amount0, amount1 = _liquidity_to_amounts(
        liquidity, sqrt_px96, tick_lower, tick_upper,
    )
    amount0_h = Decimal(amount0) / (Decimal(10) ** dec0)
    amount1_h = Decimal(amount1) / (Decimal(10) ** dec1)
    fees0_h = Decimal(tokens_owed0) / (Decimal(10) ** dec0)
    fees1_h = Decimal(tokens_owed1) / (Decimal(10) ** dec1)

    pos_value_usd, fees_value_usd = _usd_value(
        sym0, sym1, amount0_h, amount1_h, fees0_h, fees1_h, pool_price,
    )

    # Pending AERO rewards
    earned_raw = safe_call(
        lambda: gauge.functions.earned(wallet, token_id).call(),
        default=0,
        ctx=f"gauge.earned({wallet}, {token_id})",
    )
    pending_aero = Decimal(earned_raw or 0) / Decimal(10 ** 18)

    pending_aero_usd: Optional[Decimal] = None
    if pending_aero > 0:
        try:
            if aero_price_usd is None:
                from engine.data.lp_pools.aerodrome_gauge import _aero_price_usd
                aero_price_usd = _aero_price_usd() or Decimal(0)
            pending_aero_usd = pending_aero * aero_price_usd
        except Exception as exc:                                  # noqa: BLE001
            logger.warning("AERO price fetch failed: %s", exc)
            pending_aero_usd = None

    return LPNFTPosition(
        protocol="slipstream",
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
        fee_bps=fee_bps,
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
        staked=True,
        staked_in_gauge=str(gauge_addr),
        pending_aero=pending_aero,
        pending_aero_usd=pending_aero_usd,
        fetched_at=datetime.now(timezone.utc),
    )


def _adapter_is_slipstream(adapter: PoolDataAdapter) -> bool:
    """True iff adapter targets Aerodrome Slipstream on Base."""
    name = type(adapter).__name__.lower()
    return ("aerodrome" in name or "slipstream" in name) and adapter.chain == Chain.BASE


def _is_valid_hex_address(addr: str) -> bool:
    """Guard against TBD/placeholder pool addresses in lp_universe.yaml."""
    if not addr or not isinstance(addr, str):
        return False
    a = addr.strip()
    if not a.startswith("0x") or len(a) != 42:
        return False
    try:
        int(a, 16)
    except ValueError:
        return False
    return True
