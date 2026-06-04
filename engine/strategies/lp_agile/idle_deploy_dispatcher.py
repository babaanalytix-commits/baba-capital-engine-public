"""engine/strategies/lp_agile/idle_deploy_dispatcher.py

#412 — Aerodrome auto-deploy of IDLE wallet capital into existing positions.

Detects idle USDC + cbBTC sitting in the LP wallet on Base and proposes
(or executes, when live) a top-up into the matching Aerodrome Slipstream
position via the increaseLiquidity primitive in executor.py.

ROLLOUT (per Yomi's dry-run-first choice 2026-06-04):
- Default: DRY-RUN. Worker logs every proposal to lp_idle_deploy_proposals.jsonl
- LIVE: set LP_AUTO_EXECUTE_AERO_TOPUP=true in engine/.env
- Bounce the scheduler to pick up the env flip

SAFETY KNOBS:
- LP_AUTO_EXECUTE_AERO_TOPUP=false (default — dry-run)
- LP_AUTO_TOPUP_MIN_IDLE_USD=5     (don't propose unless > $5 idle of either)
- LP_AUTO_TOPUP_MAX_USD=50          (max single-tx cap)
- LP_AUTO_TOPUP_COOLDOWN_SEC=14400  (4-hour cooldown between top-ups, default)
- LP_AUTO_TOPUP_MAX_GAS_USD=2.00    (skip if gas > $2)

FLOW:
1. Read wallet idle balances (USDC, cbBTC) on Base via RPC
2. Read managed_positions for open Aerodrome USDC/cbBTC NFT
3. If wallet has idle > min AND no cooldown active:
   a. Determine ratio to add (use current pool's amount0/1 ratio)
   b. If imbalanced: swap via swap.py first to match ratio
   c. Call sign_and_send_increase_liquidity_aero(dry_run=not auto_execute)
   d. On success: write mutation row + bump cooldown timestamp
   e. Operator Telegram alert (success or proposal)

ARTIFACTS:
- engine/_signals/lp_idle_deploy_proposals.jsonl — append-only audit log
- engine/_signals/lp_idle_deploy_cooldown.json — last-fire timestamp per NFT
- engine/_signals/lp_idle_deploy_latest.json — most recent run summary

Run from wealth-ecosystem root:
    domains/multi_dex_trading_agent/.venv/bin/python3 \\
        -m engine.strategies.lp_agile.idle_deploy_dispatcher

Or via scheduler (jobs.yaml entry added separately).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.idle_deploy_dispatcher")

_REPO = Path(__file__).resolve().parents[3]
SNAPSHOT_PATH = _REPO / "ops" / "pwa" / "serve" / "lp_agile_latest.json"
MANAGED_DB = _REPO / "engine" / "_registries" / "lp_managed_positions.db"
PROPOSALS_LOG = _REPO / "engine" / "_signals" / "lp_idle_deploy_proposals.jsonl"
COOLDOWN_PATH = _REPO / "engine" / "_signals" / "lp_idle_deploy_cooldown.json"
LATEST_RESULT = _REPO / "engine" / "_signals" / "lp_idle_deploy_latest.json"

# Env knobs (all override-able)
AUTO_EXECUTE = (
    os.environ.get("LP_AUTO_EXECUTE_AERO_TOPUP", "false").lower() == "true"
)
MIN_IDLE_USD = float(os.environ.get("LP_AUTO_TOPUP_MIN_IDLE_USD", "5"))
MAX_TOPUP_USD = float(os.environ.get("LP_AUTO_TOPUP_MAX_USD", "50"))
COOLDOWN_SEC = int(os.environ.get("LP_AUTO_TOPUP_COOLDOWN_SEC", "14400"))
MAX_GAS_USD = float(os.environ.get("LP_AUTO_TOPUP_MAX_GAS_USD", "2.0"))

# Token addresses on Base (canonical)
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CBBTC_BASE = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"
USDC_DECIMALS = 6
CBBTC_DECIMALS = 8


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _log(event: dict) -> None:
    PROPOSALS_LOG.parent.mkdir(parents=True, exist_ok=True)
    event["ts_iso"] = datetime.now(timezone.utc).isoformat()
    try:
        with PROPOSALS_LOG.open("a") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except Exception as exc:
        logger.warning("[idle_deploy] log write failed: %s", exc)


def _load_cooldown() -> dict:
    try:
        if COOLDOWN_PATH.exists():
            return json.loads(COOLDOWN_PATH.read_text())
    except Exception:
        pass
    return {}


def _save_cooldown(d: dict) -> None:
    try:
        COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
        COOLDOWN_PATH.write_text(json.dumps(d, default=str))
    except Exception as exc:
        logger.warning("[idle_deploy] cooldown write failed: %s", exc)


def _read_wallet_balance() -> dict:
    """Return {usdc_atomic, usdc_usd, cbbtc_atomic, cbbtc_usd, eth_atomic}
    for the LP wallet on Base. Best-effort: returns zeros on RPC failure.
    """
    out = {
        "usdc_atomic": 0, "usdc_usd": 0.0,
        "cbbtc_atomic": 0, "cbbtc_usd": 0.0,
        "eth_atomic": 0, "eth_usd": 0.0,
    }
    try:
        from engine.strategies.lp_agile import env as _lp_env
        # env.py exposes get_lp_config (full config dict) and get_signer.
        # Build a w3 from BASE_RPC_URL ourselves; read wallet from config.
        from web3 import Web3
        cfg = _lp_env.get_lp_config()
        wallet = Web3.to_checksum_address(cfg.get("wallet_address") or "")
        if not wallet:
            return out
        rpc_url = os.environ.get(
            "BASE_RPC_URL",
            "https://base-mainnet.g.alchemy.com/v2/CYGrp_mXjU3hfX6XvOydB"
        )
        w3 = Web3(Web3.HTTPProvider(rpc_url))

        # ERC20 balanceOf via min ABI
        ERC20 = [{
            "inputs": [{"name": "owner", "type": "address"}],
            "name": "balanceOf",
            "outputs": [{"name": "", "type": "uint256"}],
            "stateMutability": "view", "type": "function",
        }]
        usdc = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_BASE), abi=ERC20,
        )
        cbbtc = w3.eth.contract(
            address=Web3.to_checksum_address(CBBTC_BASE), abi=ERC20,
        )
        out["usdc_atomic"] = int(usdc.functions.balanceOf(wallet).call())
        out["cbbtc_atomic"] = int(cbbtc.functions.balanceOf(wallet).call())
        out["eth_atomic"] = int(w3.eth.get_balance(wallet))

        # Convert to USD using a BTC price from the chain snapshot
        out["usdc_usd"] = out["usdc_atomic"] / (10 ** USDC_DECIMALS)
        btc_price = _read_btc_usd_estimate()
        out["cbbtc_usd"] = (
            out["cbbtc_atomic"] / (10 ** CBBTC_DECIMALS) * btc_price
        )
        out["eth_usd"] = out["eth_atomic"] / (10 ** 18) * _read_eth_usd_estimate()
    except Exception as exc:
        logger.warning("[idle_deploy] wallet balance read failed: %s", exc)
    return out


def _read_btc_usd_estimate() -> float:
    """Best-effort: derive cbBTC price from the chain snapshot's pool_price_now
    on the USDC/cbBTC position (slot0 ratio = USD per BTC at the time of read).
    Falls back to a sane default if unavailable."""
    try:
        d = json.loads(SNAPSHOT_PATH.read_text())
        positions = d.get("open_positions") or []
        for p in positions:
            if (p.get("pair") or "").upper() == "USDC/CBBTC":
                # pool_price_now is amount of token1 per token0; for
                # USDC/cbBTC where token0=cbBTC, this is USDC-per-cbBTC unit.
                # Snapshot shows 1.59e-5 cbBTC per 1 USDC = ~63,000 USDC/BTC
                ratio = float(p.get("pool_price_now") or 0)
                if ratio > 0:
                    # ratio = cbBTC per USDC; BTC USD = 1/ratio scaled
                    return 1.0 / ratio
    except Exception:
        pass
    return 100_000.0  # fallback estimate


def _read_eth_usd_estimate() -> float:
    """Best-effort ETH price for gas math. Not critical; falls back to default.
    Reads from lp_wallet_balance.json if available (already maintained)."""
    try:
        wb = _REPO / "engine" / "_signals" / "lp_wallet_balance.json"
        if wb.exists():
            d = json.loads(wb.read_text())
            for k in ("eth_usd_price", "eth_price_usd", "eth_usd"):
                if d.get(k):
                    return float(d[k])
    except Exception:
        pass
    return 3000.0


def _find_target_position() -> Optional[dict]:
    """Pick target via shared multi-pool picker (#420).

    Below floor → fill biggest gap first. At floor → highest APR.
    Routes through pick_target_position which both dispatchers share.

    Aerodrome swap path is USDC/cbBTC only for now; we filter at the
    picker level. Extending to other pairs needs additional swap routes.
    """
    try:
        from engine.strategies.lp_agile.idle_deploy_picker import (
            pick_target_position,
        )
        target = pick_target_position(chain="base", protocol="slipstream")
        if target is None:
            return None
        pair_upper = (target.get("pair") or "").upper()
        if "USDC" not in pair_upper or "CBBTC" not in pair_upper:
            logger.info(
                "[idle_deploy] picker chose %s but swap path is USDC/cbBTC only",
                target.get("pair"),
            )
            conn = sqlite3.connect(str(MANAGED_DB))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT * FROM managed_positions
                   WHERE chain='base' AND protocol='slipstream'
                     AND status='open'
                     AND current_nft_token_id IS NOT NULL
                     AND ((token0_symbol='USDC' AND token1_symbol='cbBTC')
                       OR (token0_symbol='cbBTC' AND token1_symbol='USDC'))
                   ORDER BY lifetime_capital_in_usd DESC LIMIT 1"""
            ).fetchone()
            conn.close()
            return dict(row) if row else None
        conn = sqlite3.connect(str(MANAGED_DB))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM managed_positions WHERE id=?",
            (target["id"],),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        result = dict(row)
        result["_picker_reason"] = target.get("reason")
        result["_picker_below_floor"] = target.get("below_floor")
        result["_picker_fill_gap_usd"] = target.get("fill_gap_usd")
        return result
    except Exception as exc:
        logger.warning("[idle_deploy] multi-pool target pick failed: %s", exc)
        return None


def _read_staking_status(nft_token_id: int) -> dict:
    """Read whether the NFT is staked in a gauge from the chain snapshot.

    Returns {"is_staked": bool, "gauge_address": str|None}.

    2026-06-04 #419 — first auto-deploy reverted because NFT 71481609 lives
    in the Aerodrome gauge, not the wallet. Executor needs to know so it
    can unstake → increase → restake.
    """
    try:
        d = json.loads(SNAPSHOT_PATH.read_text())
        for p in d.get("open_positions") or []:
            if int(p.get("nft_token_id") or 0) == int(nft_token_id):
                return {
                    "is_staked": bool(p.get("staked")),
                    "gauge_address": p.get("staked_in_gauge"),
                }
    except Exception as exc:
        logger.warning("[idle_deploy] staking-status read failed: %s", exc)
    return {"is_staked": False, "gauge_address": None}


def _read_pool_ratio() -> Optional[float]:
    """Read the current USDC:cbBTC ratio from the chain snapshot so we can
    propose a balanced top-up. Returns USDC-per-cbBTC (float) or None.
    """
    try:
        d = json.loads(SNAPSHOT_PATH.read_text())
        for p in d.get("open_positions") or []:
            if (p.get("pair") or "").upper() == "USDC/CBBTC":
                r = p.get("pool_price_now")
                if r and float(r) > 0:
                    return 1.0 / float(r)  # USDC per cbBTC
    except Exception:
        pass
    return None


# ──────────────────────────────────────────────────────────────────────
# Main flow
# ──────────────────────────────────────────────────────────────────────

def propose() -> dict:
    """Build a deploy proposal (no execution). Returns the proposal dict."""
    now_ts = time.time()
    balance = _read_wallet_balance()
    target = _find_target_position()
    cooldown = _load_cooldown()

    summary = {
        "ts_iso": datetime.now(timezone.utc).isoformat(),
        "ts": now_ts,
        "auto_execute": AUTO_EXECUTE,
        "wallet_balance": balance,
        "target": None,
        "proposal": None,
        "blocked": None,
        "executed": False,
    }

    if not target:
        summary["blocked"] = "no_eligible_target_position"
        return summary

    nft = int(target.get("current_nft_token_id") or 0)
    pair = f"{target.get('token0_symbol')}/{target.get('token1_symbol')}"
    staking = _read_staking_status(nft)
    summary["target"] = {
        "id": target["id"], "pair": pair, "nft_token_id": nft,
        "current_capital_usd": target.get("lifetime_capital_in_usd"),
        "is_staked": staking["is_staked"],
        "gauge_address": staking["gauge_address"],
    }

    # Cooldown check
    last_fire_ts = cooldown.get(str(nft), 0)
    if last_fire_ts and (now_ts - last_fire_ts) < COOLDOWN_SEC:
        remaining = COOLDOWN_SEC - (now_ts - last_fire_ts)
        summary["blocked"] = (
            f"cooldown_active ({int(remaining)}s remaining of {COOLDOWN_SEC}s)"
        )
        return summary

    # Decide if we have enough idle to act
    idle_usd_total = balance["usdc_usd"] + balance["cbbtc_usd"]
    if idle_usd_total < MIN_IDLE_USD:
        summary["blocked"] = (
            f"idle_below_min (${idle_usd_total:.2f} < ${MIN_IDLE_USD})"
        )
        return summary

    # 2026-06-04 #413: single-sided wallets now handled by auto-swap below.
    # Only block if BOTH sides are below dust (no meaningful capital to deploy)
    # AND wallet doesn't have enough idle in either side to cover gas + min size.
    if balance["usdc_usd"] < 0.50 and balance["cbbtc_usd"] < 0.50:
        summary["blocked"] = (
            f"both_sides_dust (usdc=${balance['usdc_usd']:.2f} "
            f"cbbtc=${balance['cbbtc_usd']:.2f})"
        )
        return summary

    pool_ratio_usdc_per_cbbtc = _read_pool_ratio() or 100_000.0
    # 2026-06-04 #419 — SHORTAGE-ONLY swap algorithm (rewrite of #413).
    # OLD bug: swap "wallet excess above target" — caused $144 swap when
    # only $2 was needed. Self-oscillated wallet between cycles.
    # NEW: compute SHORTAGE = max(0, deposit_need - wallet_balance) and
    # only swap that shortage. Sides already sufficient = no swap.
    # Effect: with $25 USDC + $167 cbBTC, deposit need is $25 each → both
    # sufficient → no swap → just deploy. Excess cbBTC stays idle and
    # gets deployed in the next 4h cycle.
    total_idle_usd = balance["usdc_usd"] + balance["cbbtc_usd"]
    deploy_total = min(MAX_TOPUP_USD, total_idle_usd * 0.98)  # 2% slip+gas buffer
    target_each_side_usd = deploy_total / 2.0

    swap_plan_summary = None
    SWAP_DELTA_MIN_USD = 1.0  # tighter floor; $1 swap on $0.30 gas is OK
    usdc_short = max(0.0, target_each_side_usd - balance["usdc_usd"])
    cbbtc_short = max(0.0, target_each_side_usd - balance["cbbtc_usd"])

    if usdc_short > SWAP_DELTA_MIN_USD:
        # Short USDC, swap from cbBTC (if cbBTC has excess beyond its own need)
        cbbtc_available_for_swap = max(
            0.0, balance["cbbtc_usd"] - target_each_side_usd,
        )
        if cbbtc_available_for_swap >= usdc_short:
            cbbtc_in_human = usdc_short / pool_ratio_usdc_per_cbbtc
            swap_plan_summary = {
                "direction": "cbBTC→USDC",
                "swap_usd": round(usdc_short, 2),
                "amount_in_human": round(cbbtc_in_human, 8),
                "expected_out_human": round(usdc_short, 4),
                "reason": (
                    f"USDC short ${usdc_short:.2f} "
                    f"(need ${target_each_side_usd:.2f}, "
                    f"have ${balance['usdc_usd']:.2f})"
                ),
            }
        else:
            # Wallet too imbalanced — scale deploy down to what cbBTC can support
            achievable = balance["cbbtc_usd"] * 0.98 * 2.0  # cbBTC-bound
            deploy_total = min(deploy_total, achievable)
            target_each_side_usd = deploy_total / 2.0
            # Recompute — should now be OK
    elif cbbtc_short > SWAP_DELTA_MIN_USD:
        usdc_available_for_swap = max(
            0.0, balance["usdc_usd"] - target_each_side_usd,
        )
        if usdc_available_for_swap >= cbbtc_short:
            swap_plan_summary = {
                "direction": "USDC→cbBTC",
                "swap_usd": round(cbbtc_short, 2),
                "amount_in_human": round(cbbtc_short, 4),
                "expected_out_human": round(
                    cbbtc_short / pool_ratio_usdc_per_cbbtc, 8,
                ),
                "reason": (
                    f"cbBTC short ${cbbtc_short:.2f} "
                    f"(need ${target_each_side_usd:.2f}, "
                    f"have ${balance['cbbtc_usd']:.2f})"
                ),
            }
        else:
            achievable = balance["usdc_usd"] * 0.98 * 2.0
            deploy_total = min(deploy_total, achievable)
            target_each_side_usd = deploy_total / 2.0
    # else: both sides sufficient → no swap, just deploy

    # Project the post-swap deploy amounts
    deploy_usdc_usd = target_each_side_usd
    deploy_cbbtc_usd = target_each_side_usd
    deploy_usdc_atomic = int(deploy_usdc_usd * (10 ** USDC_DECIMALS))
    deploy_cbbtc_atomic = int(
        (deploy_cbbtc_usd / pool_ratio_usdc_per_cbbtc) * (10 ** CBBTC_DECIMALS)
    )

    # Higher gas budget when we have to do the unstake→increase→restake dance
    # (3 extra txs ~$0.40 each on Base).
    base_gas = 0.60 if swap_plan_summary else 0.30
    if staking["is_staked"]:
        base_gas += 1.20  # withdraw + nft.approve + deposit

    summary["proposal"] = {
        "nft_token_id": nft,
        "pair": pair,
        "deploy_usd_total": round(deploy_total, 2),
        "pre_swap": swap_plan_summary,
        "amount0_desired_atomic": deploy_usdc_atomic,
        "amount1_desired_atomic": deploy_cbbtc_atomic,
        "amount0_desired_human": round(deploy_usdc_usd, 4),
        "amount1_desired_human": round(
            deploy_cbbtc_atomic / (10 ** CBBTC_DECIMALS), 8,
        ),
        "estimated_gas_usd": base_gas,
        "pool_ratio_usdc_per_cbbtc": pool_ratio_usdc_per_cbbtc,
        # #419 — staking-aware deployment
        "is_staked": bool(staking["is_staked"]),
        "gauge_address": staking["gauge_address"],
    }
    return summary


def tick() -> dict:
    """One run of the dispatcher. Logs proposal; executes only when
    LP_AUTO_EXECUTE_AERO_TOPUP=true AND nothing blocks."""
    started = time.time()
    summary = propose()
    summary["latency_ms"] = int((time.time() - started) * 1000)

    # Always log the proposal (even when blocked — that's the audit trail)
    _log({"action": "proposal", **summary})

    # DRY-RUN exit
    if not AUTO_EXECUTE:
        summary["mode"] = "dry_run"
        _persist_summary(summary)
        return summary

    if summary.get("blocked") or not summary.get("proposal"):
        summary["mode"] = "auto_execute_blocked"
        _persist_summary(summary)
        return summary

    # LIVE EXECUTE
    p = summary["proposal"]
    # 2026-06-04 #413: pre-swap if wallet is imbalanced
    pre_swap = p.get("pre_swap")
    if pre_swap:
        try:
            from decimal import Decimal as _D
            from engine.strategies.lp_agile.swap import SwapPlan, execute_swap
            from engine.strategies.lp_agile.types import Chain
            if pre_swap["direction"] == "USDC→cbBTC":
                plan = SwapPlan(
                    swap_needed=True,
                    token_in_addr=USDC_BASE,
                    token_out_addr=CBBTC_BASE,
                    amount_in_human=_D(str(pre_swap["amount_in_human"])),
                    expected_amount_out_human=_D(
                        str(pre_swap["expected_out_human"])
                    ),
                    reason=pre_swap["reason"],
                )
            else:
                plan = SwapPlan(
                    swap_needed=True,
                    token_in_addr=CBBTC_BASE,
                    token_out_addr=USDC_BASE,
                    amount_in_human=_D(str(pre_swap["amount_in_human"])),
                    expected_amount_out_human=_D(
                        str(pre_swap["expected_out_human"])
                    ),
                    reason=pre_swap["reason"],
                )
            swap_result = execute_swap(
                plan=plan, chain=Chain.BASE, tick_spacing=100,
            )
            summary["swap_result"] = {
                "success": swap_result.success,
                "tx_hash": swap_result.tx_hash,
                "error": swap_result.error,
                "amount_in": str(swap_result.amount_in_human),
                "amount_out": str(swap_result.amount_out_human),
            }
            if not swap_result.success:
                summary["mode"] = "swap_failed"
                _log({"action": "swap_failed", **summary})
                _persist_summary(summary)
                return summary
            # 2026-06-04 #419 — longer wait + nonce check after swap.
            # Last night's live test hit "in-flight transaction limit
            # reached for delegated accounts" on the next approve tx
            # because we only waited 3s. The RPC apparently needs more
            # time even after wait_for_transaction_receipt returns.
            # Wait 20s + verify nonce has advanced before proceeding.
            time.sleep(20)
            try:
                from engine.strategies.lp_agile import env as _e
                from web3 import Web3 as _W3
                _rpc = os.environ.get(
                    "BASE_RPC_URL",
                    "https://base-mainnet.g.alchemy.com/v2/CYGrp_mXjU3hfX6XvOydB",
                )
                _w3 = _W3(_W3.HTTPProvider(_rpc))
                _signer = _e.get_signer()
                # Poll nonce until it's at least the expected value
                # (no concurrent pending txs from the previous swap)
                for _attempt in range(6):
                    pending = _w3.eth.get_transaction_count(_signer.address, "pending")
                    confirmed = _w3.eth.get_transaction_count(_signer.address, "latest")
                    if pending == confirmed:
                        break  # no in-flight txs
                    logger.info(
                        "[idle_deploy] pending=%d confirmed=%d — waiting "
                        "for in-flight tx to settle (attempt %d/6)",
                        pending, confirmed, _attempt + 1,
                    )
                    time.sleep(10)
                summary["nonce_settled_check"] = {
                    "pending": pending, "confirmed": confirmed,
                }
            except Exception as _exc:                              # noqa: BLE001
                logger.warning(
                    "[idle_deploy] nonce settle check errored: %s — "
                    "proceeding optimistically", _exc,
                )
                summary["nonce_settled_check"] = {"error": str(_exc)}
            fresh = _read_wallet_balance()
            # Recompute deploy atomic amounts from fresh balance, capped at
            # the planned target so we don't accidentally exceed MAX
            p["amount0_desired_atomic"] = min(
                p["amount0_desired_atomic"], int(fresh["usdc_atomic"] * 0.99),
            )
            p["amount1_desired_atomic"] = min(
                p["amount1_desired_atomic"], int(fresh["cbbtc_atomic"] * 0.99),
            )
        except Exception as exc:
            summary["mode"] = "swap_exception"
            summary["swap_result"] = {"error": f"exception: {exc}"}
            _log({"action": "swap_exception", **summary})
            _persist_summary(summary)
            return summary

    try:
        from engine.strategies.lp_agile.executor import (
            sign_and_send_increase_liquidity_aero,
        )
        result = sign_and_send_increase_liquidity_aero(
            nft_token_id=int(p["nft_token_id"]),
            amount0_desired=int(p["amount0_desired_atomic"]),
            amount1_desired=int(p["amount1_desired_atomic"]),
            slippage_pct=0.5,
            deadline_sec=600,
            dry_run=False,
            is_staked=bool(p.get("is_staked")),
            gauge_address=p.get("gauge_address"),
        )
        summary["execute_result"] = {
            "executed": result.executed,
            "tx_hash": result.tx_hash,
            "gas_usd": result.actual_gas_usd,
            "liquidity_before": str(result.liquidity_before),
            "liquidity_after": str(result.liquidity_after),
            "error": result.error,
        }
        summary["executed"] = bool(result.executed)
        if result.executed:
            cd = _load_cooldown()
            cd[str(p["nft_token_id"])] = time.time()
            _save_cooldown(cd)
            _log({"action": "executed", **summary})
            _alert_telegram(
                f"✅ <b>LP idle-deploy fired</b>\n"
                f"NFT: {p['nft_token_id']}\n"
                f"Amount: ${p['deploy_usd_total']:.2f}\n"
                f"Tx: <code>{result.tx_hash}</code>"
            )
        else:
            _log({"action": "execute_failed", **summary})
    except Exception as exc:
        summary["execute_result"] = {"error": f"exception: {exc}"}
        summary["executed"] = False
        _log({"action": "execute_exception", **summary})

    summary["mode"] = "live_execute"
    _persist_summary(summary)
    return summary


def _persist_summary(s: dict) -> None:
    try:
        LATEST_RESULT.parent.mkdir(parents=True, exist_ok=True)
        LATEST_RESULT.write_text(json.dumps(s, indent=2, default=str))
    except Exception:
        pass


def _alert_telegram(message: str) -> None:
    try:
        from engine.telegram.client import send
        send("signal", key=f"idle_deploy:{int(time.time())}",
             text=message, parse_mode="HTML")
    except Exception:
        pass


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    s = tick()
    # Operator-friendly stdout summary
    print(json.dumps(s, indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
