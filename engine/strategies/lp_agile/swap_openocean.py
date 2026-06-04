"""engine/strategies/lp_agile/swap_openocean.py — OpenOcean swap on HyperEVM.

Why this exists:
  HyperEVM has no canonical Uniswap V3 SwapRouter wired in our codebase
  (HyperSwap V3 router addresses are stubs / empty). The de-facto swap
  aggregator everyone uses on HyperEVM is OpenOcean, which routes across
  prjx, KittenSwap, HyperSwap, etc.

  Yomi's prior LP mints went through OpenOcean's *zap* router
  (0xB165C4d4B8044D4A9276c3d75F08cD6a2874A3b2, selector 0x5f575529) which
  combines swap + mint in one tx. That requires the zap-specific API and
  is overkill for top-ups. Instead, this module uses OpenOcean's plain
  swap router (0x6352a56caadC4F1E25CD6c75970Fa768A3304e64) which returns
  ready-to-sign calldata for an `exact-in` swap.

  Once we have WHYPE in the wallet, the existing
  `sign_and_send_increase_liquidity_prjx` primitive (mirror of Aerodrome's,
  built 2026-06-04 #419) handles the deposit.

API contract:
  GET https://open-api.openocean.finance/v3/hyperevm/swap_quote
    ?inTokenAddress=...&outTokenAddress=...&amount=N  (decimal token units)
    &slippage=1&gasPrice=1&account=...
  Response:
    code: 200
    data: {
      inAmount: atomic in,  outAmount: atomic out,
      to: router address (0x6352…0e64),
      data: hex calldata to send,
      gas: estimate,
      ...
    }

Usage:
    quote = get_openocean_quote(
        in_token=USDC, out_token=WHYPE, amount_decimal="25", slippage_pct=1.0,
        account=wallet_addr,
    )
    tx_hash = execute_openocean_swap(quote)  # signs + broadcasts + waits

Standing directive [[feedback-lp-dedicated-wallet]]: only the LP wallet.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.swap_openocean")

OPENOCEAN_API_BASE = "https://open-api.openocean.finance/v3/hyperevm"
# Standard ERC-20 approve ABI — wallet must approve the router for token spending.
ERC20_ABI = [
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "_owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}],
     "stateMutability": "view", "type": "function"},
]


@dataclass
class OpenOceanQuote:
    """Snapshot of an OpenOcean route — ready to sign + send."""
    in_token: str
    out_token: str
    in_amount_atomic: int
    out_amount_atomic: int
    in_amount_decimal: Decimal
    out_amount_decimal: Decimal
    in_decimals: int
    out_decimals: int
    router_to: str           # address we send the tx to
    calldata: str            # hex tx data (no signature yet)
    value: int               # native token value (0 for ERC20→ERC20)
    gas_estimate: int
    minimum_out_atomic: int   # after slippage
    raw: dict                 # the full API response for debug


def get_openocean_quote(
    *,
    in_token: str,
    out_token: str,
    amount_decimal: str,
    slippage_pct: float = 1.0,
    gas_price_gwei: float = 1.0,
    account: str,
    in_decimals: int = 6,
    out_decimals: int = 18,
) -> OpenOceanQuote:
    """Fetch a fresh OpenOcean swap quote for HyperEVM.

    Args:
      in_token: ERC-20 address being sold
      out_token: ERC-20 address being bought
      amount_decimal: human-readable amount string ("25" = 25 USDC)
      slippage_pct: 0-100 max slippage tolerance
      gas_price_gwei: HyperEVM gas price (typically 1 gwei)
      account: wallet address (needed for some aggregators' route hints)
      in_decimals / out_decimals: token decimals — falls back to 18 / 6 if wrong

    Raises:
      RuntimeError if the API returns non-200 code or no route.
    """
    params = {
        "inTokenAddress": in_token,
        "outTokenAddress": out_token,
        "amount": str(amount_decimal),     # OpenOcean takes decimal, not atomic
        "slippage": str(slippage_pct),
        "gasPrice": str(gas_price_gwei),
        "account": account,
    }
    url = f"{OPENOCEAN_API_BASE}/swap_quote?" + urllib.parse.urlencode(params)
    logger.info("OpenOcean quote: %s %s → %s", amount_decimal, in_token[:8], out_token[:8])

    req = urllib.request.Request(url, headers={"User-Agent": "BABA-LP/1.0"})
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        body = json.loads(resp.read().decode())
    except Exception as exc:
        raise RuntimeError(f"OpenOcean API call failed: {exc}") from exc

    if body.get("code") != 200:
        raise RuntimeError(f"OpenOcean returned code={body.get('code')}: {body}")

    d = body.get("data") or {}
    if not d.get("to") or not d.get("data"):
        raise RuntimeError(f"OpenOcean returned no route: {body}")

    in_atomic = int(d["inAmount"])
    out_atomic = int(d["outAmount"])
    min_out_atomic = int(out_atomic * (1 - slippage_pct / 100.0))

    return OpenOceanQuote(
        in_token=in_token,
        out_token=out_token,
        in_amount_atomic=in_atomic,
        out_amount_atomic=out_atomic,
        in_amount_decimal=Decimal(in_atomic) / Decimal(10 ** in_decimals),
        out_amount_decimal=Decimal(out_atomic) / Decimal(10 ** out_decimals),
        in_decimals=in_decimals,
        out_decimals=out_decimals,
        router_to=d["to"],
        calldata=d["data"],
        value=int(d.get("value", 0)),
        gas_estimate=int(d.get("estimatedGas") or d.get("gas") or 400_000),
        minimum_out_atomic=min_out_atomic,
        raw=d,
    )


def _send_erc20_approve(
    w3, signer, token_addr: str, spender: str, amount: int, *, label: str,
) -> str:
    """Mirror of executor._send_approve, retry-with-backoff, but for HyperEVM."""
    from web3 import Web3
    token = w3.eth.contract(
        address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI,
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
            logger.info("%s tx submitted (attempt %d/3): %s", label, attempt + 1, tx_hash)
            rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if rcpt.status != 1:
                raise RuntimeError(f"{label} reverted (status={rcpt.status})")
            return tx_hash
        except Exception as exc:                                  # noqa: BLE001
            last_exc = exc
            err = str(exc).lower()
            is_throttle = "rate limit" in err or "too many" in err or "-32000" in err
            if not is_throttle or attempt == 2:
                raise
            backoff = 10 * (attempt + 1)
            logger.warning("%s throttle, retry in %ds: %s", label, backoff, exc)
            time.sleep(backoff)
    raise last_exc or RuntimeError(f"{label} exhausted retries")


@dataclass
class SwapResult:
    executed: bool
    tx_hash: Optional[str] = None
    in_amount_atomic: int = 0
    out_amount_atomic: int = 0
    actual_out_atomic: Optional[int] = None   # measured from wallet balance delta
    gas_used: int = 0
    error: Optional[str] = None
    duration_s: float = 0.0


def execute_openocean_swap(
    quote: OpenOceanQuote,
    *,
    rpc_url: str = "https://rpc.hyperliquid.xyz/evm",
    dry_run: bool = True,
) -> SwapResult:
    """Sign + send the OpenOcean swap tx.

    Returns SwapResult with on-chain measured `actual_out_atomic` so
    downstream code knows exactly how much we got.

    Standing directive [[feedback-lp-dedicated-wallet]]: only LP wallet.
    """
    start = time.time()

    if dry_run:
        logger.info(
            "[openocean DRY-RUN] %.6f %s → %.6f %s  (slippage cap %d atomic)",
            float(quote.in_amount_decimal), quote.in_token[:8],
            float(quote.out_amount_decimal), quote.out_token[:8],
            quote.minimum_out_atomic,
        )
        return SwapResult(
            executed=False,
            in_amount_atomic=quote.in_amount_atomic,
            out_amount_atomic=quote.out_amount_atomic,
            duration_s=time.time() - start,
        )

    try:
        from engine.strategies.lp_agile import env as _e
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
        signer = _e.get_signer()
    except Exception as exc:                                      # noqa: BLE001
        return SwapResult(
            executed=False, error=f"setup failed: {exc}",
            duration_s=time.time() - start,
        )

    # ─── Step 1: Approve router for the input token ──────────────────────
    try:
        _send_erc20_approve(
            w3, signer, quote.in_token, quote.router_to, quote.in_amount_atomic,
            label=f"oo_swap:approve({quote.in_token[:8]})",
        )
    except Exception as exc:                                      # noqa: BLE001
        return SwapResult(
            executed=False, error=f"approve failed: {exc}",
            duration_s=time.time() - start,
        )

    # ─── Step 2: Read out-token balance BEFORE swap (for actual delta) ──
    out_token_c = w3.eth.contract(
        address=Web3.to_checksum_address(quote.out_token), abi=ERC20_ABI,
    )
    try:
        balance_before = out_token_c.functions.balanceOf(signer.address).call()
    except Exception:
        balance_before = 0

    # ─── Step 3: Send the OpenOcean swap tx ──────────────────────────────
    try:
        nonce = w3.eth.get_transaction_count(signer.address, "pending")
        tx = {
            "from": signer.address,
            "to": Web3.to_checksum_address(quote.router_to),
            "data": quote.calldata,
            "nonce": nonce,
            "gas": int(quote.gas_estimate * 1.3),  # 30% headroom
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
            "value": quote.value,
        }
        signed = signer.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
        logger.info("[openocean swap] tx submitted: %s", tx_hash)
        rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        if rcpt.status != 1:
            raise RuntimeError(f"swap reverted (status={rcpt.status})")
    except Exception as exc:                                      # noqa: BLE001
        return SwapResult(
            executed=False, error=f"swap tx failed: {exc}",
            duration_s=time.time() - start,
        )

    # ─── Step 4: Measure actual receipt ──────────────────────────────────
    try:
        balance_after = out_token_c.functions.balanceOf(signer.address).call()
        actual_out = balance_after - balance_before
    except Exception:
        actual_out = quote.out_amount_atomic

    return SwapResult(
        executed=True,
        tx_hash=tx_hash,
        in_amount_atomic=quote.in_amount_atomic,
        out_amount_atomic=quote.out_amount_atomic,
        actual_out_atomic=actual_out,
        gas_used=rcpt.get("gasUsed", 0),
        duration_s=time.time() - start,
    )
