"""engine/strategies/lp_agile/prjx_adapter.py — prjx single-tx range adjust (#108).

DESIGN STATUS: SCAFFOLDED, AWAITING 1-2 MORE SAMPLE TXS for full triangulation
of ExecuteParams field meanings. The function selector is confirmed via
Yomi's tx 0x4ed841...10fc:

  Router:    0x855165ee5fa4eca8c30d11bec095f206a28ec14b
  Selector:  0xdc3253a0
  Signature: execute(uint256, (uint8, address, uint256, uint256, uint256,
             uint256, bytes, uint256, uint256, bytes, uint128, uint128,
             uint24, int24, int24, uint128, uint256, uint256, uint256,
             address, address, bool, bytes, bytes))

Decoded from Yomi's update tx (tokenId=465275):
  [0] tokenId           = 465275
  [3] addr (token0)     = 0x5555…5555 (WHYPE)
  [14] uint24            = 3000 (fee tier 0.30%)
  [15] int24 tickLower   = 2's complement encoded
  [16] int24 tickUpper
  [20] uint256 deadline  = 1780040311 (unix ts)
  [21] address           = 0x0108…8482 (Yomi LP wallet)
  [22] address           = 0x0108…8482 (recipient again or owner)

UNKNOWN (need 2nd tx):
  - [2] uint8: action type? always 0 in Yomi's tx
  - [4-5] uint256, uint256: amount0_min, amount1_min?
  - [6,7,8,9] uint256: offsets/lengths for bytes arrays
  - [10] uint256: 0x81efe03e59357e (~$0.003 if scaled)
  - [11] uint256: 0x320 = 800
  - [12,13] uint128: 0xff…ff (max uint128 — probably amount0_max, amount1_max)
  - [17,18,19] uint256: more amounts/params
  - [23] bool: false
  - [24] bytes: empty

UNTIL THESE ARE TRIANGULATED, the adapter exposes:
  - `is_supported`: True (router + selector confirmed)
  - `estimate_gas_for_rebalance(token_id)`: returns 800_000 (validated from Yomi tx)
  - `build_execute_calldata(token_id, new_tick_lower, new_tick_upper, ...)`:
       raises NotImplementedError until field meanings confirmed

The trigger engine (#109) can already use `estimate_gas_for_rebalance` for
payback-day math.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.prjx_adapter")


# Constants confirmed via direct RPC + on-chain calldata inspection
PRJX_ROUTER = "0x855165ee5fa4eca8c30d11bec095f206a28ec14b"
PRJX_EXECUTE_SELECTOR = "0xdc3253a0"
PRJX_POSITION_MANAGER = "0xeaD19AE861c29bBb2101E834922B2FEee69B9091"
# Per Yomi's tx 0x4ed841...10fc: gas used = 795,110
EST_GAS_PER_REBALANCE = 800_000

# Slippage / safety knobs
DEFAULT_DEADLINE_SEC = int(os.environ.get("PRJX_REBAL_DEADLINE_SEC", "120"))
DEFAULT_SLIPPAGE_BPS = int(os.environ.get("PRJX_REBAL_SLIPPAGE_BPS", "200"))


def is_supported() -> bool:
    """Whether the prjx single-tx range-adjust is available.

    True once the execute() ABI is fully triangulated. For now: True for
    informational purposes (gas estimate), False for actual tx building.
    """
    return True


def estimate_gas_for_rebalance(token_id: Optional[int] = None) -> int:
    """Gas estimate for one execute() rebalance call. Constant for now;
    refine per-pool once we have 5+ historical txs."""
    return EST_GAS_PER_REBALANCE


def expected_gas_cost_usd(gas_price_gwei: float = 1.0,
                          hype_price_usd: float = 40.0) -> float:
    """Convert the gas estimate to USD assuming HyperEVM gas price + HYPE
    price. HyperEVM typical gas price ≤ 1 gwei.
    """
    gas_eth = (EST_GAS_PER_REBALANCE * gas_price_gwei * 1e-9)
    return gas_eth * hype_price_usd


def build_execute_calldata(*, reference_calldata,
                            token_id: int,
                            new_tick_lower: int,
                            new_tick_upper: int,
                            deadline_ts: int,
                            ) -> bytes:
    """V1 TEMPLATE PATCHER. Triangulated 2026-05-30 via cross-tx diff of two
    Yomi range-adjust txs. Layout of the 42-word execute() calldata:

      word[0]  = tokenId                          (VAR)
      word[14] = feeTier                          (VAR per pool)
      word[15] = tickLower (int24 sign-extended)  (VAR)
      word[16] = tickUpper                        (VAR)
      word[20] = deadline_ts                      (VAR)
      word[5,9,10,17-19,36-38] = pool-state amts  (VAR per call)
      all other words = CONST per pool

    V1 only changes tokenId, tickLower, tickUpper, deadline. Pool-state
    amount fields are copied from the reference tx → V1 only works for
    rebalances CLOSE to the reference range on the SAME pool. Different
    pools or large-jump moves need V2 (eth_abi encode-from-scratch).

    `reference_calldata` accepts bytes or a 0x-prefixed hex string.
    """
    if isinstance(reference_calldata, str):
        s = reference_calldata
        if s.startswith("0x"):
            s = s[2:]
        reference_calldata = bytes.fromhex(s)
    if not isinstance(reference_calldata, (bytes, bytearray)):
        raise TypeError("reference_calldata must be bytes or hex string")
    cd = bytearray(reference_calldata)
    if cd[:4].hex() != "dc3253a0":
        raise ValueError(f"selector {cd[:4].hex()} != execute() 0xdc3253a0")
    body_len = len(cd) - 4
    if body_len % 32 != 0:
        raise ValueError(f"body length {body_len} not 32-byte aligned")
    n_words = body_len // 32
    if n_words != 42:
        raise ValueError(f"got {n_words} words; expected 42")

    def _slice(idx: int) -> slice:
        start = 4 + idx * 32
        return slice(start, start + 32)

    def _patch_uint256(idx: int, value: int) -> None:
        if value < 0 or value >= (1 << 256):
            raise ValueError(f"uint256 OOR at word {idx}: {value}")
        cd[_slice(idx)] = value.to_bytes(32, byteorder="big")

    def _patch_int24_packed(idx: int, value: int) -> None:
        """Sign-extend an int24 into a full 32-byte word."""
        if value < -(1 << 23) or value >= (1 << 23):
            raise ValueError(f"int24 OOR at word {idx}: {value}")
        full = ((1 << 256) + value) if value < 0 else value
        cd[_slice(idx)] = full.to_bytes(32, byteorder="big")

    _patch_uint256(0, token_id)
    _patch_int24_packed(15, new_tick_lower)
    _patch_int24_packed(16, new_tick_upper)
    _patch_uint256(20, deadline_ts)
    return bytes(cd)


# Reference txs (template sources for build_execute_calldata).
# Indexed by fee tier; add new entries as we encounter different pools.
REFERENCE_TXS: dict[int, str] = {
    3000: "0x40fd799045e1ca6ea31b2d73da61018ef5fd268c1220a114168f79e649d92cd9",
    500:  "0x12cca11052ef5b968a9f99e645aac7e8cd83be6001564181f9649ddf32fe241b",
}


def fetch_reference_calldata(fee_tier: int, rpc_url: str | None = None) -> bytes:
    """Fetch a known-good reference tx's calldata for the given fee tier.
    Used by rebalance_in_place to template-patch new calldata."""
    import json as _json, urllib.request as _req
    tx_hash = REFERENCE_TXS.get(fee_tier)
    if not tx_hash:
        raise ValueError(
            f"no reference tx for fee_tier {fee_tier}. "
            f"Available: {list(REFERENCE_TXS.keys())}"
        )
    url = rpc_url or os.environ.get(
        "HYPER_EVM_RPC", "https://rpc.hyperliquid.xyz/evm"
    )
    payload = {
        "jsonrpc": "2.0", "method": "eth_getTransactionByHash",
        "params": [tx_hash], "id": 1,
    }
    req = _req.Request(url, data=_json.dumps(payload).encode(),
                       headers={"Content-Type": "application/json"},
                       method="POST")
    with _req.urlopen(req, timeout=15) as resp:
        data = _json.loads(resp.read())
    tx = data.get("result")
    if not tx:
        raise RuntimeError(f"RPC returned no tx for {tx_hash}")
    inp = tx.get("input", "0x")
    if inp.startswith("0x"):
        inp = inp[2:]
    return bytes.fromhex(inp)


def rebalance_in_place(*, token_id: int, new_tick_lower: int,
                        new_tick_upper: int,
                        fee_tier: int = 3000,
                        slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
                        deadline_sec: int = DEFAULT_DEADLINE_SEC,
                        dry_run: bool = True) -> dict:
    """High-level entry — caller asks 'rebalance NFT token_id into the new
    range'. Returns a plan dict; dry-run includes built calldata for
    inspection. Live mode builds calldata + would send tx (TX-send wire-up
    is the next step — Yomi to confirm signer + nonce path).

    V1 PATCHER scope (2026-05-30): only safe for rebalances on the same
    pool fee tier as a known reference tx. See REFERENCE_TXS.
    """
    import time as _time
    fee_tier = int(fee_tier)
    deadline_ts = int(_time.time()) + int(deadline_sec)
    plan = {
        "router": PRJX_ROUTER,
        "selector": PRJX_EXECUTE_SELECTOR,
        "token_id": int(token_id),
        "new_tick_lower": int(new_tick_lower),
        "new_tick_upper": int(new_tick_upper),
        "fee_tier": fee_tier,
        "slippage_bps": int(slippage_bps),
        "deadline_ts": deadline_ts,
        "est_gas": EST_GAS_PER_REBALANCE,
        "est_gas_cost_usd": expected_gas_cost_usd(),
        "status": "BUILT_V1_TEMPLATE_PATCHER",
    }
    # Build calldata via template patch
    try:
        if fee_tier not in REFERENCE_TXS:
            plan["status"] = "REFUSED_NO_REFERENCE_FOR_FEE_TIER"
            plan["error"] = (
                f"no reference tx for fee_tier {fee_tier}. "
                f"Add to REFERENCE_TXS first."
            )
            return plan
        ref_cd = fetch_reference_calldata(fee_tier)
        new_cd = build_execute_calldata(
            reference_calldata=ref_cd,
            token_id=int(token_id),
            new_tick_lower=int(new_tick_lower),
            new_tick_upper=int(new_tick_upper),
            deadline_ts=deadline_ts,
        )
        plan["calldata_len"] = len(new_cd)
        plan["calldata_hex"] = "0x" + new_cd.hex()
    except Exception as exc:                                       # noqa: BLE001
        plan["status"] = "REFUSED_BUILD_FAILED"
        plan["error"] = f"{type(exc).__name__}: {exc}"
        return plan
    if dry_run:
        return plan
    # Live send wired-up is next-step work — until then, refuse to send.
    logger.warning(
        "[prjx] rebalance_in_place live-send not yet wired for token %d — "
        "calldata BUILT but tx-send blocked pending signer integration",
        token_id,
    )
    plan["status"] = "CALLDATA_BUILT_TX_SEND_NOT_WIRED"
    return plan


if __name__ == "__main__":
    # Smoke test
    logging.basicConfig(level=logging.INFO)
    print("prjx adapter scaffolded.")
    print(f"  router: {PRJX_ROUTER}")
    print(f"  selector: {PRJX_EXECUTE_SELECTOR}")
    print(f"  est gas: {EST_GAS_PER_REBALANCE:,}")
    print(f"  est gas cost (1 gwei × $40 HYPE): ${expected_gas_cost_usd():.4f}")
    print(f"  is_supported: {is_supported()}")
    plan = rebalance_in_place(token_id=465275, new_tick_lower=-25000,
                              new_tick_upper=-17000, dry_run=True)
    import json as _json
    print("  dry-run plan:", _json.dumps(plan, indent=2))
