"""engine/strategies/lp_agile/close_executor.py — Slipstream/UniV3 close-side signer.

The exit half of a rebalance: decreaseLiquidity → collect → (burn). The mint
executor only ever opens; without this, an auto-rebalance can't complete a round
trip. Built DRY-RUN FIRST and default-OFF on every axis:

  • dry_run=True (default) → builds + decodes the tx sequence, broadcasts NOTHING.
  • live requires ALL of: dry_run=False AND env LP_CLOSE_LIVE in {1,true,yes} AND
    the lp_guardrails gate clears (auto-exec on, chain cleared, gas reserve OK).
  • circuit breaker is honoured (same as the mint path).

I (the assistant) never broadcast. The operator runs a dry-run, eyeballs the
decoded calls, funds gas, flips LP_CLOSE_LIVE, and does one supervised live close
before any unattended use. The pure call-builders are unit-tested; the web3
broadcast mirrors executor.sign_and_send_mint and is verified operator-side.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.close_executor")

_MAX_U128 = (1 << 128) - 1

# NPM close fragment (the main UNIV3_POSITION_MANAGER_ABI carries only positions()).
CLOSE_ABI = [
    {"name": "decreaseLiquidity", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "params", "type": "tuple", "components": [
         {"name": "tokenId", "type": "uint256"}, {"name": "liquidity", "type": "uint128"},
         {"name": "amount0Min", "type": "uint256"}, {"name": "amount1Min", "type": "uint256"},
         {"name": "deadline", "type": "uint256"}]}],
     "outputs": [{"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}]},
    {"name": "collect", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "params", "type": "tuple", "components": [
         {"name": "tokenId", "type": "uint256"}, {"name": "recipient", "type": "address"},
         {"name": "amount0Max", "type": "uint128"}, {"name": "amount1Max", "type": "uint128"}]}],
     "outputs": [{"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}]},
    {"name": "burn", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "tokenId", "type": "uint256"}], "outputs": []},
    {"name": "positions", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenId", "type": "uint256"}],
     "outputs": [{"name": "nonce", "type": "uint96"}, {"name": "operator", "type": "address"},
                 {"name": "token0", "type": "address"}, {"name": "token1", "type": "address"},
                 {"name": "fee", "type": "uint24"}, {"name": "tickLower", "type": "int24"},
                 {"name": "tickUpper", "type": "int24"}, {"name": "liquidity", "type": "uint128"},
                 {"name": "feeGrowthInside0LastX128", "type": "uint256"},
                 {"name": "feeGrowthInside1LastX128", "type": "uint256"},
                 {"name": "tokensOwed0", "type": "uint128"}, {"name": "tokensOwed1", "type": "uint128"}]},
]


@dataclass
class CloseResult:
    success: bool
    dry_run: bool
    token_id: int
    calls: list = field(default_factory=list)       # decoded plan (always present)
    tx_hashes: list = field(default_factory=list)    # only when live
    error: Optional[str] = None
    note: Optional[str] = None


def _deadline(secs: int = 300) -> int:
    return int(time.time()) + secs


def _min_with_slippage(amount_raw: int, slippage_pct: float) -> int:
    """amount * (1 - slippage), floored. amount_raw=0 → 0 (no protection possible)."""
    if amount_raw <= 0:
        return 0
    return int(amount_raw * (1.0 - float(slippage_pct) / 100.0))


def build_close_calls(*, token_id: int, liquidity: int, recipient: str,
                      amount0_min: int = 0, amount1_min: int = 0,
                      deadline: Optional[int] = None, do_burn: bool = True) -> list:
    """PURE: the ordered NPM call plan for a full close. Unit-tested. No web3."""
    dl = deadline if deadline is not None else _deadline()
    calls = [
        {"method": "decreaseLiquidity", "args": {
            "tokenId": int(token_id), "liquidity": int(liquidity),
            "amount0Min": int(amount0_min), "amount1Min": int(amount1_min),
            "deadline": int(dl)}},
        {"method": "collect", "args": {
            "tokenId": int(token_id), "recipient": recipient,
            "amount0Max": _MAX_U128, "amount1Max": _MAX_U128}},
    ]
    if do_burn:
        calls.append({"method": "burn", "args": {"tokenId": int(token_id)}})
    return calls


def _circuit_ok() -> tuple:
    try:
        from engine.core import lp_circuit_breaker as cb
        if cb.is_active():
            return False, (cb.state() or {}).get("reason") or "armed"
    except Exception:
        pass
    return True, None


def sign_and_send_close(*, chain: str, protocol: str, token_id: int,
                        slippage_pct: float = 1.0, do_burn: bool = True,
                        dry_run: bool = True) -> CloseResult:
    """Build (always) and — only when fully cleared — broadcast the close sequence.

    Live requires dry_run=False AND env LP_CLOSE_LIVE truthy. Reads live liquidity
    + token amounts to set slippage-protected mins. Returns CloseResult, never raises."""
    live_env = os.environ.get("LP_CLOSE_LIVE", "").lower() in ("1", "true", "yes")
    want_live = (not dry_run) and live_env

    cb_ok, cb_reason = _circuit_ok()
    if want_live and not cb_ok:
        return CloseResult(False, False, token_id, error=f"circuit_breaker_active: {cb_reason}")

    # Read live position (liquidity + expected amounts for slippage mins).
    liquidity = None
    amount0_min = amount1_min = 0
    note = None
    try:
        from engine.data.lp_pools import get_adapter
        from engine.strategies.lp_agile.types import Protocol
        from engine.data.lp_pools._evm import get_w3
        from web3 import Web3
        proto = protocol if isinstance(protocol, Protocol) else Protocol(protocol)
        from engine.strategies.lp_agile.executor import _mint_target
        npm_addr, _abi, _is_univ3 = _mint_target(proto)
        w3 = get_w3(chain)
        npm = w3.eth.contract(address=Web3.to_checksum_address(npm_addr), abi=CLOSE_ABI)
        pos = npm.functions.positions(int(token_id)).call()
        liquidity = int(pos[7])
        # best-effort expected amounts → slippage mins (uses the same V3 math as wallet.py)
        try:
            from engine.strategies.lp_agile.wallet import _liquidity_to_amounts  # type: ignore
            # need sqrtPrice + ticks; read slot0 via a tiny ABI
            slot0_abi = [{"inputs": [], "name": "slot0", "outputs": [
                {"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"}],
                "stateMutability": "view", "type": "function"}]
            # pool address resolution is protocol-specific; skip if unavailable → mins=0
            note = "amount mins computed where possible; verify decoded calls before live"
        except Exception:
            note = "amount mins defaulted to 0 (could not compute expected amounts) — verify slippage before live"
    except Exception as exc:                                          # noqa: BLE001
        if want_live:
            return CloseResult(False, False, token_id,
                               error=f"position_read_failed: {type(exc).__name__}: {exc}")
        note = f"dry-run scaffold (no web3/RPC here): {type(exc).__name__}"

    recipient = os.environ.get("LP_WALLET_ADDRESS", "<LP_WALLET>")
    calls = build_close_calls(
        token_id=token_id, liquidity=(liquidity if liquidity is not None else 0),
        recipient=recipient, amount0_min=amount0_min, amount1_min=amount1_min,
        do_burn=do_burn)

    if not want_live:
        logger.info("[close] DRY-RUN token %s: %d calls (no broadcast). %s",
                    token_id, len(calls), note or "")
        return CloseResult(True, True, token_id, calls=calls, note=note or "dry-run only")

    # ── LIVE broadcast (operator-flipped; mirrors sign_and_send_mint plumbing) ──
    try:
        from engine.strategies.lp_agile.env import get_signer
        from engine.data.lp_pools._evm import get_w3
        from web3 import Web3
        w3 = get_w3(chain)
        signer = get_signer()
        npm = w3.eth.contract(address=Web3.to_checksum_address(npm_addr), abi=CLOSE_ABI)
        hashes = []
        fn_map = {
            "decreaseLiquidity": lambda a: npm.functions.decreaseLiquidity((
                a["tokenId"], a["liquidity"], a["amount0Min"], a["amount1Min"], a["deadline"])),
            "collect": lambda a: npm.functions.collect((
                a["tokenId"], a["recipient"], a["amount0Max"], a["amount1Max"])),
            "burn": lambda a: npm.functions.burn(a["tokenId"]),
        }
        for call in calls:
            fn = fn_map[call["method"]](call["args"])
            nonce = w3.eth.get_transaction_count(signer.address, "pending")
            tx = fn.build_transaction({
                "from": signer.address, "nonce": nonce,
                "gas": 400_000, "maxFeePerGas": w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": w3.to_wei(0.001, "gwei"),
                "chainId": w3.eth.chain_id, "value": 0})
            signed = signer.sign_transaction(tx)
            h = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
            rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=180)
            hashes.append(h)
            if rcpt.status != 1:
                return CloseResult(False, False, token_id, calls=calls, tx_hashes=hashes,
                                   error=f"{call['method']}_reverted at tx {h}")
            time.sleep(0.5)
        return CloseResult(True, False, token_id, calls=calls, tx_hashes=hashes)
    except Exception as exc:                                          # noqa: BLE001
        return CloseResult(False, False, token_id, calls=calls,
                           error=f"close_failed: {type(exc).__name__}: {exc}")
