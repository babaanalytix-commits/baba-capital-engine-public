"""engine/strategies/lp_agile/gauge_stake.py — stake/unstake Slipstream NFTs.

Why this exists:
  Minting a Slipstream LP NFT (via NPM) gives the position trading-fee yield,
  but does NOT enrol it in the gauge for AERO emissions. To earn the full
  yield breakdown the LP scanner advertises, the NFT must be deposited into
  the pool's CLGauge.

Mechanics verified on-chain 2026-05-24:
  - Slipstream gauges are EIP-1167 clones delegating to the canonical
    CLGauge implementation.
  - `stakedContains(wallet, tokenId)` is the trustless way to verify
    enrolment.
  - `deposit(tokenId)` transfers the NFT into the gauge (custodial).
    Requires prior `npm.setApprovalForAll(gauge, true)` or
    `npm.approve(gauge, tokenId)`.
  - `withdraw(tokenId)` returns the NFT to the depositor wallet. NO time
    lock. Accrued AERO must be claimed via `getReward(tokenId)` either
    before withdraw or as a separate tx.
  - `earned(wallet, tokenId)` returns currently-claimable AERO (uint256, 18d).
  - veAERO (4-year vote-escrow lock) is a separate contract and does NOT
    apply to LP-gauge staking. LP staking is liquid in both directions.

Standing directive: per [[feedback-lp-dedicated-wallet]] this module only
operates on the LP wallet (LP_WALLET_ADDRESS / LP_WALLET_PRIVATE_KEY env vars).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from web3 import Web3

from engine.data.lp_pools._evm import Chain, get_w3, safe_call
from engine.data.lp_pools.aerodrome_gauge import (
    AERODROME_VOTER,
    AERO_TOKEN,
    VOTER_ABI,
    _aero_price_usd,
)

logger = logging.getLogger("engine.strategies.lp_agile.gauge_stake")


# CLGauge ABI subset — read + write primitives we need
CLGAUGE_ABI = [
    # views
    {"inputs": [], "name": "rewardRate", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "depositor", "type": "address"}, {"name": "tokenId", "type": "uint256"}],
     "name": "stakedContains", "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "account", "type": "address"}, {"name": "tokenId", "type": "uint256"}],
     "name": "earned", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    # writes
    {"inputs": [{"name": "tokenId", "type": "uint256"}],
     "name": "deposit", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "tokenId", "type": "uint256"}],
     "name": "withdraw", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "tokenId", "type": "uint256"}],
     "name": "getReward", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]

# Minimal NPM ABI for approval — gauge takes custody of the NFT
NPM_APPROVAL_ABI = [
    {"inputs": [{"name": "operator", "type": "address"}, {"name": "approved", "type": "bool"}],
     "name": "setApprovalForAll", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"}, {"name": "operator", "type": "address"}],
     "name": "isApprovedForAll", "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "tokenId", "type": "uint256"}],
     "name": "ownerOf", "outputs": [{"name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
]


@dataclass
class StakeStatus:
    """Trustless snapshot of staking state for a Slipstream LP NFT."""
    token_id: int
    pool_address: str
    gauge_address: Optional[str]
    nft_owner: str                          # 0x0 if doesn't exist
    is_staked: bool                          # True if gauge holds the NFT
    earned_aero: Decimal                     # claimable AERO (0 if not staked)
    earned_aero_usd: Decimal                 # USD value at current AERO price

    @property
    def fully_owned(self) -> bool:
        """True if NFT is either in wallet OR staked-and-owned by us."""
        return self.is_staked or self.nft_owner != "0x0000000000000000000000000000000000000000"


def get_gauge_for_pool(pool_address: str) -> Optional[str]:
    """Look up the Slipstream gauge address for a pool. None if no gauge."""
    w3 = get_w3(Chain.BASE)
    voter = w3.eth.contract(address=AERODROME_VOTER, abi=VOTER_ABI)
    addr = safe_call(
        lambda: voter.functions.gauges(Web3.to_checksum_address(pool_address)).call(),
        default=None,
        ctx=f"voter.gauges({pool_address})",
    )
    if not addr or int(addr, 16) == 0:
        return None
    return Web3.to_checksum_address(addr)


def check_stake_status(
    token_id: int, pool_address: str, wallet_address: str,
) -> StakeStatus:
    """Trustless read of staking state for a position. Does not transact."""
    w3 = get_w3(Chain.BASE)
    wallet = Web3.to_checksum_address(wallet_address)
    gauge_addr = get_gauge_for_pool(pool_address)

    # Read NFT owner via NPM
    from engine.data.lp_pools.aerodrome import AERO_SLIPSTREAM_NPM
    npm = w3.eth.contract(address=AERO_SLIPSTREAM_NPM, abi=NPM_APPROVAL_ABI)
    nft_owner = safe_call(
        lambda: npm.functions.ownerOf(token_id).call(),
        default="0x0000000000000000000000000000000000000000",
        ctx=f"npm.ownerOf({token_id})",
    )

    is_staked = False
    earned_aero = Decimal(0)
    if gauge_addr:
        gauge = w3.eth.contract(address=gauge_addr, abi=CLGAUGE_ABI)
        # If NFT is staked, gauge is the owner — check both ways for robustness
        is_staked = (
            nft_owner.lower() == gauge_addr.lower()
            or bool(safe_call(
                lambda: gauge.functions.stakedContains(wallet, token_id).call(),
                default=False,
                ctx=f"gauge.stakedContains({wallet}, {token_id})",
            ))
        )
        if is_staked:
            earned_raw = safe_call(
                lambda: gauge.functions.earned(wallet, token_id).call(),
                default=0,
                ctx=f"gauge.earned({wallet}, {token_id})",
            )
            earned_aero = Decimal(earned_raw or 0) / Decimal(10**18)

    aero_price = _aero_price_usd() if earned_aero > 0 else Decimal(0)
    earned_usd = earned_aero * aero_price

    return StakeStatus(
        token_id=token_id,
        pool_address=pool_address,
        gauge_address=gauge_addr,
        nft_owner=nft_owner,
        is_staked=is_staked,
        earned_aero=earned_aero,
        earned_aero_usd=earned_usd,
    )


def stake_nft(
    token_id: int, pool_address: str, *, dry_run: bool = True,
) -> dict:
    """Stake a Slipstream LP NFT in its pool's gauge.

    Flow:
      1. Verify NFT is owned by LP wallet.
      2. Verify gauge exists.
      3. Set approval (if not already set) — one-time per gauge.
      4. Call gauge.deposit(tokenId).
      5. Verify is_staked == True after.

    dry_run=True returns the planned txs without signing.
    """
    from engine.strategies.lp_agile.env import get_lp_config, get_signer

    cfg = get_lp_config()
    if not cfg.get("wallet_address"):
        return {"ok": False, "error": "LP_WALLET_ADDRESS not configured"}

    wallet = Web3.to_checksum_address(cfg["wallet_address"])
    w3 = get_w3(Chain.BASE)

    status = check_stake_status(token_id, pool_address, wallet)
    if not status.gauge_address:
        return {"ok": False, "error": "no_gauge_for_pool", "pool": pool_address}
    if status.is_staked:
        return {
            "ok": True, "skipped": "already_staked",
            "gauge": status.gauge_address, "token_id": token_id,
        }
    if status.nft_owner.lower() != wallet.lower():
        return {
            "ok": False, "error": "wallet_does_not_own_nft",
            "owner": status.nft_owner, "wallet": wallet,
        }

    from engine.data.lp_pools.aerodrome import AERO_SLIPSTREAM_NPM
    npm = w3.eth.contract(address=AERO_SLIPSTREAM_NPM, abi=NPM_APPROVAL_ABI)
    gauge = w3.eth.contract(address=status.gauge_address, abi=CLGAUGE_ABI)

    approved = safe_call(
        lambda: npm.functions.isApprovedForAll(wallet, status.gauge_address).call(),
        default=False,
        ctx=f"npm.isApprovedForAll({wallet}, {status.gauge_address})",
    )
    plan = {
        "wallet": wallet,
        "token_id": token_id,
        "gauge": status.gauge_address,
        "needs_approval": not approved,
        "txs_planned": (1 if not approved else 0) + 1,
    }
    if dry_run:
        plan["mode"] = "dry_run"
        return {"ok": True, "plan": plan}

    # LIVE path — get_signer() returns an eth_account.Account; the private
    # key never appears as a dict value (env module's safety design).
    try:
        acct = get_signer()
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if acct.address.lower() != wallet.lower():
        return {"ok": False, "error": "private_key_does_not_match_wallet_address"}

    out = {"wallet": wallet, "token_id": token_id, "gauge": status.gauge_address}

    if not approved:
        approve_tx = npm.functions.setApprovalForAll(status.gauge_address, True).build_transaction({
            "from": wallet,
            "nonce": w3.eth.get_transaction_count(wallet, "pending"),
            "gas": 80_000,
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": w3.eth.max_priority_fee,
            "chainId": 8453,
        })
        signed = acct.sign_transaction(approve_tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        r = w3.eth.wait_for_transaction_receipt(h, timeout=60)
        out["approve_tx"] = h.hex()
        out["approve_status"] = r.status
        if r.status != 1:
            return {"ok": False, "error": "approval_reverted", **out}
        time.sleep(0.5)

    # Dynamic gas estimate + 50% headroom. Slipstream CLGauge.deposit()
    # touches many storage slots for reward accounting; observed real-world
    # usage 540k+. Hardcoding 300k caused our first attempt to OOG-revert
    # silently ("no data") — see RCA 2026-05-24.
    try:
        est = gauge.functions.deposit(token_id).estimate_gas({"from": wallet})
        gas_limit = int(est * 1.5)
    except Exception:                                         # noqa: BLE001
        gas_limit = 800_000  # conservative fallback if estimate fails
    deposit_tx = gauge.functions.deposit(token_id).build_transaction({
        "from": wallet,
        "nonce": w3.eth.get_transaction_count(wallet, "pending"),
        "gas": gas_limit,
        "maxFeePerGas": w3.eth.gas_price * 2,
        "maxPriorityFeePerGas": w3.eth.max_priority_fee,
        "chainId": 8453,
    })
    signed = acct.sign_transaction(deposit_tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    r = w3.eth.wait_for_transaction_receipt(h, timeout=60)
    out["deposit_tx"] = h.hex()
    out["deposit_status"] = r.status
    out["deposit_gas_used"] = r.gasUsed
    out["deposit_gas_limit"] = gas_limit

    # Trustless verify
    post = check_stake_status(token_id, pool_address, wallet)
    out["verified_staked"] = post.is_staked

    return {"ok": post.is_staked, **out}


def unstake_nft(
    token_id: int, pool_address: str, *, claim_rewards: bool = True, dry_run: bool = True,
) -> dict:
    """Unstake (withdraw) an LP NFT from the gauge. Optionally claim AERO first.

    No time lock — withdraw is instant. AERO accrued is paid out via
    getReward(); calling withdraw() also auto-collects per the canonical
    Aerodrome impl, but we explicitly call getReward first for clarity in
    the cost ledger.
    """
    from engine.strategies.lp_agile.env import get_lp_config, get_signer

    cfg = get_lp_config()
    wallet = Web3.to_checksum_address(cfg["wallet_address"])
    w3 = get_w3(Chain.BASE)

    status = check_stake_status(token_id, pool_address, wallet)
    if not status.is_staked:
        return {"ok": True, "skipped": "not_staked", "token_id": token_id}

    plan = {
        "wallet": wallet,
        "token_id": token_id,
        "gauge": status.gauge_address,
        "claimable_aero": float(status.earned_aero),
        "claimable_aero_usd": float(status.earned_aero_usd),
        "txs_planned": (1 if claim_rewards else 0) + 1,
    }
    if dry_run:
        plan["mode"] = "dry_run"
        return {"ok": True, "plan": plan}

    try:
        acct = get_signer()
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    gauge = w3.eth.contract(address=status.gauge_address, abi=CLGAUGE_ABI)
    out = {"wallet": wallet, "token_id": token_id, "gauge": status.gauge_address}

    if claim_rewards and status.earned_aero > 0:
        claim_tx = gauge.functions.getReward(token_id).build_transaction({
            "from": wallet,
            "nonce": w3.eth.get_transaction_count(wallet, "pending"),
            "gas": 200_000,
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": w3.eth.max_priority_fee,
            "chainId": 8453,
        })
        signed = acct.sign_transaction(claim_tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        r = w3.eth.wait_for_transaction_receipt(h, timeout=60)
        out["claim_tx"] = h.hex()
        out["claim_status"] = r.status
        out["claimed_aero"] = float(status.earned_aero)
        time.sleep(0.5)

    try:
        est = gauge.functions.withdraw(token_id).estimate_gas({"from": wallet})
        wg_limit = int(est * 1.5)
    except Exception:                                         # noqa: BLE001
        wg_limit = 800_000
    withdraw_tx = gauge.functions.withdraw(token_id).build_transaction({
        "from": wallet,
        "nonce": w3.eth.get_transaction_count(wallet, "pending"),
        "gas": wg_limit,
        "maxFeePerGas": w3.eth.gas_price * 2,
        "maxPriorityFeePerGas": w3.eth.max_priority_fee,
        "chainId": 8453,
    })
    signed = acct.sign_transaction(withdraw_tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    r = w3.eth.wait_for_transaction_receipt(h, timeout=60)
    out["withdraw_tx"] = h.hex()
    out["withdraw_status"] = r.status

    # Trustless verify
    post = check_stake_status(token_id, pool_address, wallet)
    out["verified_unstaked"] = not post.is_staked

    return {"ok": not post.is_staked, **out}
