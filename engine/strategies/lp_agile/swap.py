"""engine/strategies/lp_agile/swap.py — autonomous pre-mint swap helper.

Per [[feedback-lp-autonomous-swaps]] standing directive: from trade #2
onward, the agent owns the full open sequence including swaps. This module
implements the swap-on-Aerodrome-Slipstream primitive needed to land an LP
position when wallet doesn't already hold both legs in the right ratio.

Sequence wrapped by the live executor:
  needed_split() → swap(token_in→token_out, amount) → wait → mint

Slippage: defaults to 0.5% (conservative for stable/stable-ish pairs).
Gas: ~$0.05 on Base.
Trustless verify: re-reads balances after swap, surfaces realised slippage
                  as cost-ledger input.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from web3 import Web3

from engine.data.lp_pools._abis import ERC20_ABI
from engine.data.lp_pools._evm import get_w3
from engine.strategies.lp_agile.env import get_signer
from engine.strategies.lp_agile.types import Chain

logger = logging.getLogger("engine.strategies.lp_agile.swap")


# ---------------------------------------------------------------------------
# Slipstream SwapRouter on Base (verified live 2026-05-24)
# ---------------------------------------------------------------------------

SLIPSTREAM_SWAP_ROUTER = Web3.to_checksum_address(
    "0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5"
)

# exactInputSingle for Slipstream — tickSpacing replaces fee field
SLIPSTREAM_SWAP_ABI = [
    {
        "inputs": [{
            "components": [
                {"name": "tokenIn", "type": "address"},
                {"name": "tokenOut", "type": "address"},
                {"name": "tickSpacing", "type": "int24"},
                {"name": "recipient", "type": "address"},
                {"name": "deadline", "type": "uint256"},
                {"name": "amountIn", "type": "uint256"},
                {"name": "amountOutMinimum", "type": "uint256"},
                {"name": "sqrtPriceLimitX96", "type": "uint160"},
            ],
            "name": "params", "type": "tuple",
        }],
        "name": "exactInputSingle",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function",
    },
]

ERC20_APPROVE_ABI = [
    {"inputs": [
        {"name": "spender", "type": "address"},
        {"name": "amount", "type": "uint256"},
     ], "name": "approve",
     "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [
        {"name": "owner", "type": "address"},
        {"name": "spender", "type": "address"},
     ], "name": "allowance",
     "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SwapResult:
    success: bool
    tx_hash: Optional[str]
    amount_in_human: Decimal
    amount_out_human: Decimal
    realised_slippage_pct: Optional[Decimal]
    gas_cost_usd_est: Optional[Decimal]
    error: Optional[str]
    duration_s: float


# ---------------------------------------------------------------------------
# Pre-mint balance computation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SwapPlan:
    """What swap (if any) is needed to satisfy a target mint."""
    swap_needed: bool
    token_in_addr: Optional[str]
    token_out_addr: Optional[str]
    amount_in_human: Decimal
    expected_amount_out_human: Decimal
    reason: str


def compute_swap_for_mint(
    *, t0_addr: str, t1_addr: str,
    dec0: int, dec1: int,
    bal0_human: Decimal, bal1_human: Decimal,
    desired0_human: Decimal, desired1_human: Decimal,
    base_price_usd: Decimal, is_stable_0: bool,
) -> SwapPlan:
    """Compute the swap needed to satisfy desired amounts at the time of mint.

    Returns SwapPlan(swap_needed=False, ...) if balances are already sufficient
    on BOTH sides.
    """
    short0 = max(Decimal(0), desired0_human - bal0_human)
    short1 = max(Decimal(0), desired1_human - bal1_human)

    if short0 == 0 and short1 == 0:
        return SwapPlan(
            swap_needed=False, token_in_addr=None, token_out_addr=None,
            amount_in_human=Decimal(0), expected_amount_out_human=Decimal(0),
            reason="balances already sufficient",
        )

    # One side short, the other has surplus → swap surplus to short side.
    if short0 > 0 and short1 == 0:
        # Need more token0, swap token1 → token0
        # If stable_0: token0 is USDC, token1 is volatile. Swap volatile → USDC.
        if is_stable_0:
            # 1 token1 = base_price_usd token0 (USDC)
            amount_in = short0 / base_price_usd
        else:
            # token0 is volatile, token1 is stable
            # 1 token1 (stable, $1) = 1/base_price_usd token0
            amount_in = short0 * base_price_usd
        return SwapPlan(
            swap_needed=True,
            token_in_addr=t1_addr, token_out_addr=t0_addr,
            amount_in_human=amount_in * Decimal("1.01"),  # 1% buffer
            expected_amount_out_human=short0,
            reason=f"need {short0:.8f} more token0, swap from surplus token1",
        )
    if short1 > 0 and short0 == 0:
        if is_stable_0:
            # short token1 (volatile), have surplus token0 (stable USDC)
            amount_in = short1 * base_price_usd
        else:
            amount_in = short1 / base_price_usd
        return SwapPlan(
            swap_needed=True,
            token_in_addr=t0_addr, token_out_addr=t1_addr,
            amount_in_human=amount_in * Decimal("1.01"),
            expected_amount_out_human=short1,
            reason=f"need {short1:.8f} more token1, swap from surplus token0",
        )

    # Both sides short → wallet undersized for target position
    return SwapPlan(
        swap_needed=False, token_in_addr=None, token_out_addr=None,
        amount_in_human=Decimal(0), expected_amount_out_human=Decimal(0),
        reason=f"BOTH sides short (t0:{short0:.6f}, t1:{short1:.8f}) — "
               f"wallet too small for this position size",
    )


# ---------------------------------------------------------------------------
# Execute the swap
# ---------------------------------------------------------------------------


def execute_swap(
    *, plan: SwapPlan, chain: Chain = Chain.BASE,
    tick_spacing: int = 100, slippage_pct: Decimal = Decimal("0.005"),
) -> SwapResult:
    """Sign + send the swap. Trustless verify via post-swap balance diff."""
    start = time.time()
    if not plan.swap_needed:
        return SwapResult(
            success=True, tx_hash=None,
            amount_in_human=Decimal(0), amount_out_human=Decimal(0),
            realised_slippage_pct=None, gas_cost_usd_est=Decimal(0),
            error=None, duration_s=time.time() - start,
        )

    try:
        signer = get_signer()
    except Exception as e:                                # noqa: BLE001
        return SwapResult(
            success=False, tx_hash=None,
            amount_in_human=Decimal(0), amount_out_human=Decimal(0),
            realised_slippage_pct=None, gas_cost_usd_est=None,
            error=f"signer_load_failed: {e}",
            duration_s=time.time() - start,
        )

    w3 = get_w3(chain)
    wallet = signer.address
    t_in = Web3.to_checksum_address(plan.token_in_addr)
    t_out = Web3.to_checksum_address(plan.token_out_addr)

    # Read decimals
    t_in_c = w3.eth.contract(address=t_in, abi=ERC20_ABI)
    t_out_c = w3.eth.contract(address=t_out, abi=ERC20_ABI)
    dec_in = t_in_c.functions.decimals().call()
    dec_out = t_out_c.functions.decimals().call()

    amount_in_atomic = int(plan.amount_in_human * Decimal(10 ** dec_in))
    amount_out_min = int(
        plan.expected_amount_out_human * (Decimal(1) - slippage_pct)
        * Decimal(10 ** dec_out)
    )

    # 1. Ensure SwapRouter approval on token_in
    t_in_approve = w3.eth.contract(address=t_in, abi=ERC20_APPROVE_ABI)
    cur_allow = t_in_approve.functions.allowance(wallet, SLIPSTREAM_SWAP_ROUTER).call()
    if cur_allow < amount_in_atomic:
        logger.info("swap: approving %d on token_in for SwapRouter", amount_in_atomic)
        nonce = w3.eth.get_transaction_count(wallet, "pending")
        tx = t_in_approve.functions.approve(
            SLIPSTREAM_SWAP_ROUTER, amount_in_atomic,
        ).build_transaction({
            "from": wallet, "nonce": nonce, "gas": 100_000,
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": w3.to_wei(0.001, "gwei"),
            "chainId": w3.eth.chain_id, "value": 0,
        })
        signed = signer.sign_transaction(tx)
        approve_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
        rcpt = w3.eth.wait_for_transaction_receipt(approve_hash, timeout=120)
        if rcpt.status != 1:
            return SwapResult(
                success=False, tx_hash=None,
                amount_in_human=plan.amount_in_human, amount_out_human=Decimal(0),
                realised_slippage_pct=None, gas_cost_usd_est=None,
                error=f"swap_approve_reverted: {approve_hash}",
                duration_s=time.time() - start,
            )
        time.sleep(0.5)

    # 2. Build + send swap tx
    router = w3.eth.contract(address=SLIPSTREAM_SWAP_ROUTER, abi=SLIPSTREAM_SWAP_ABI)
    deadline = int(time.time()) + 300
    swap_params = (
        t_in, t_out, int(tick_spacing), wallet, deadline,
        amount_in_atomic, amount_out_min, 0,    # sqrtPriceLimitX96=0 = no limit
    )
    fn = router.functions.exactInputSingle(swap_params)
    bal_out_before = t_out_c.functions.balanceOf(wallet).call()
    try:
        nonce = w3.eth.get_transaction_count(wallet, "pending")
        tx = fn.build_transaction({
            "from": wallet, "nonce": nonce, "gas": 300_000,
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": w3.to_wei(0.001, "gwei"),
            "chainId": w3.eth.chain_id, "value": 0,
        })
        signed = signer.sign_transaction(tx)
        swap_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
        logger.info("swap submitted: %s", swap_hash)
        rcpt = w3.eth.wait_for_transaction_receipt(swap_hash, timeout=180)
        if rcpt.status != 1:
            return SwapResult(
                success=False, tx_hash=swap_hash,
                amount_in_human=plan.amount_in_human, amount_out_human=Decimal(0),
                realised_slippage_pct=None, gas_cost_usd_est=None,
                error=f"swap_reverted (status={rcpt.status})",
                duration_s=time.time() - start,
            )
        bal_out_after = t_out_c.functions.balanceOf(wallet).call()
        received_atomic = bal_out_after - bal_out_before
        received_human = Decimal(received_atomic) / Decimal(10 ** dec_out)
        slippage = None
        if plan.expected_amount_out_human > 0:
            slippage = ((plan.expected_amount_out_human - received_human)
                        / plan.expected_amount_out_human)
        gas_cost_est = Decimal(rcpt.gasUsed * w3.eth.gas_price) / Decimal(10**18) * Decimal("2070")
        return SwapResult(
            success=True, tx_hash=swap_hash,
            amount_in_human=plan.amount_in_human, amount_out_human=received_human,
            realised_slippage_pct=slippage, gas_cost_usd_est=gas_cost_est,
            error=None, duration_s=time.time() - start,
        )
    except Exception as e:                                # noqa: BLE001
        return SwapResult(
            success=False, tx_hash=None,
            amount_in_human=plan.amount_in_human, amount_out_human=Decimal(0),
            realised_slippage_pct=None, gas_cost_usd_est=None,
            error=f"swap_failed: {type(e).__name__}: {e}",
            duration_s=time.time() - start,
        )
