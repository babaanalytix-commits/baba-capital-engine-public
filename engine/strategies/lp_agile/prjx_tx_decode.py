"""engine/strategies/lp_agile/prjx_tx_decode.py — decode prjx execute() txs.

Helper to triangulate the prjx router's execute() ABI by decoding existing
on-chain calldata. Takes a transaction hash, fetches it from HyperEVM RPC,
splits the calldata into the 24-field tuple, and prints each field with our
current hypothesis label.

Usage:
    cd ~/baba/wealth-ecosystem
    python3 -m engine.strategies.lp_agile.prjx_tx_decode \\
        0x4ed841...10fc

You can pass multiple tx hashes and the script compares fields across them.
Constant fields are flagged; variable ones are candidates for "amount" or
"tickLower"-style parameters.

This is the inverse of build_execute_calldata(): give it a known-good tx,
it tells you what each field IS. Once we've decoded 2-3 different rebalance
operations, all 24 fields will be confirmed and build_execute_calldata()
can be implemented properly.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
from typing import List, Optional

logger = logging.getLogger("engine.strategies.lp_agile.prjx_tx_decode")


HYPER_EVM_RPC = os.environ.get(
    "HYPER_EVM_RPC", "https://rpc.hyperliquid.xyz/evm"
)
PRJX_EXECUTE_SELECTOR = "0xdc3253a0"

# Current best-guess labels (24-tuple inside execute(uint256 tokenId, tuple))
# Field index = position in the inner tuple (the first uint256 is tokenId,
# the rest is the tuple). Update labels as we learn.
FIELD_LABELS = [
    "[0] tokenId",
    "[1] uint8 (action type? — 0 in update?)",
    "[2] address token0 (WHYPE for HYPE pool?)",
    "[3] uint256 amount0_desired?",
    "[4] uint256 amount1_desired?",
    "[5] uint256 amount0_min?",
    "[6] uint256 amount1_min?",
    "[7] bytes data0_offset?",
    "[8] uint256 ??? ",
    "[9] uint256 ??? ",
    "[10] bytes data1_offset?",
    "[11] uint128 amount0_max?",
    "[12] uint128 amount1_max?",
    "[13] uint24 feeTier (3000 = 0.30%)",
    "[14] int24 tickLower",
    "[15] int24 tickUpper",
    "[16] uint128 liquidity?",
    "[17] uint256 ??? ",
    "[18] uint256 ??? ",
    "[19] uint256 ??? ",
    "[20] uint256 deadline (unix ts)",
    "[21] address recipient",
    "[22] address owner",
    "[23] bool ??? (false in update?)",
]


def _rpc_get_tx(tx_hash: str) -> Optional[dict]:
    """POST to HyperEVM RPC to fetch a transaction by hash."""
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getTransactionByHash",
        "params": [tx_hash],
        "id": 1,
    }
    req = urllib.request.Request(
        HYPER_EVM_RPC,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data.get("result")
    except Exception as exc:                                       # noqa: BLE001
        logger.error("RPC failed for %s: %s", tx_hash, exc)
        return None


def _split_calldata(calldata: str) -> tuple[str, list[str]]:
    """Strip the 0x and selector, return (selector, list of 32-byte words)."""
    if calldata.startswith("0x"):
        calldata = calldata[2:]
    selector = "0x" + calldata[:8]
    body = calldata[8:]
    # Split into 32-byte (64 hex char) words
    words = [body[i:i + 64] for i in range(0, len(body), 64)]
    return selector, words


def _hex_to_int(hex_str: str) -> int:
    """Treat hex word as a signed int24 / int256 appropriately. Default
    unsigned; caller can use the signed conversion for int24 / int256 fields."""
    return int(hex_str, 16)


def _hex_to_int24(hex_str: str) -> int:
    """Decode an int24 stored in a 32-byte word (right-padded). The int24 is
    in the low 3 bytes; sign-extend if MSB is set."""
    # Take last 6 hex chars (3 bytes), interpret as signed int24
    last_3_bytes = hex_str[-6:]
    raw = int(last_3_bytes, 16)
    if raw >= 0x800000:  # sign bit set
        raw -= 0x1000000
    return raw


def _word_label(idx: int) -> str:
    if 0 <= idx < len(FIELD_LABELS):
        return FIELD_LABELS[idx]
    return f"[{idx}] ???"


def decode_tx(tx_hash: str) -> dict:
    """Fetch + decode one prjx execute() transaction."""
    tx = _rpc_get_tx(tx_hash)
    if not tx:
        return {"error": "RPC fetch failed", "tx_hash": tx_hash}
    calldata = tx.get("input", "0x")
    selector, words = _split_calldata(calldata)
    result = {
        "tx_hash": tx_hash,
        "from": tx.get("from"),
        "to": tx.get("to"),
        "value": tx.get("value"),
        "selector": selector,
        "n_words": len(words),
        "fields": {},
    }
    if selector.lower() != PRJX_EXECUTE_SELECTOR.lower():
        result["warning"] = (
            f"selector mismatch — expected {PRJX_EXECUTE_SELECTOR}, "
            f"got {selector}"
        )
    for i, w in enumerate(words):
        val_uint = _hex_to_int(w)
        # Heuristic for int24 fields
        val_int24 = _hex_to_int24(w) if (i in (14, 15)) else None
        addr_candidate = (
            "0x" + w[-40:] if w.startswith("0" * 24) else None
        )
        entry = {
            "label": _word_label(i),
            "raw": "0x" + w,
            "uint": val_uint,
        }
        if val_uint == 0:
            entry["hint"] = "zero (or unset)"
        elif val_uint == (1 << 128) - 1:
            entry["hint"] = "max uint128 — likely amount_max"
        elif val_uint == (1 << 256) - 1:
            entry["hint"] = "max uint256"
        elif val_int24 is not None:
            entry["int24"] = val_int24
        if addr_candidate and len(addr_candidate) == 42:
            entry["address"] = addr_candidate
        result["fields"][i] = entry
    return result


def diff_decoded(decoded_list: List[dict]) -> dict:
    """Compare N decoded txs side-by-side. Flag fields that VARY (variable)
    vs CONSTANT (likely contract address / wallet)."""
    if not decoded_list:
        return {}
    n_fields = max(len(d.get("fields", {})) for d in decoded_list)
    cmp = {}
    for i in range(n_fields):
        vals = [d["fields"].get(i, {}).get("raw") for d in decoded_list]
        cmp[i] = {
            "label": _word_label(i),
            "constant_across_txs": len(set(vals)) == 1,
            "values": vals,
        }
    return cmp


def main(argv=None) -> int:
    global HYPER_EVM_RPC
    import argparse
    ap = argparse.ArgumentParser(prog="prjx_tx_decode")
    ap.add_argument("tx_hashes", nargs="+",
                    help="One or more prjx execute() transaction hashes")
    ap.add_argument("--rpc", default=HYPER_EVM_RPC,
                    help="HyperEVM RPC URL")
    args = ap.parse_args(argv)
    HYPER_EVM_RPC = args.rpc

    decoded = []
    for h in args.tx_hashes:
        print(f"\n{'=' * 70}\n DECODING {h}\n{'=' * 70}")
        d = decode_tx(h)
        if "error" in d:
            print(f"  ERROR: {d['error']}")
            continue
        if "warning" in d:
            print(f"  ⚠ {d['warning']}")
        print(f"  from:     {d['from']}")
        print(f"  to:       {d['to']}")
        print(f"  selector: {d['selector']}")
        print(f"  words:    {d['n_words']}")
        for i, f in d["fields"].items():
            line = f"  {f['label']:<48s}  uint={f['uint']}"
            if "int24" in f:
                line += f"  int24={f['int24']}"
            if "address" in f:
                line += f"  addr={f['address']}"
            if "hint" in f:
                line += f"  ← {f['hint']}"
            print(line)
        decoded.append(d)

    if len(decoded) >= 2:
        print(f"\n{'=' * 70}\n CROSS-TX COMPARISON ({len(decoded)} txs)\n{'=' * 70}")
        cmp = diff_decoded(decoded)
        for i, info in cmp.items():
            tag = "CONST" if info["constant_across_txs"] else "VAR  "
            print(f"  [{i:2d}] {tag}  {info['label']:<48s}  {info['values']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
