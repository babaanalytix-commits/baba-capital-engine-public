"""engine/strategies/lp_agile/idle_deploy_dispatcher_prjx.py — auto-deploy
idle USDC on HyperEVM into existing prjx LP positions.

Mirror of idle_deploy_dispatcher.py (Aerodrome) with HyperEVM-specific bits:
  - OpenOcean swap router for the USDC → WHYPE balancing step
  - prjx NPM contract on HyperEVM (0xeaD19AE861c29bBb2101E834922B2FEee69B9091)
  - HyperEVM RPC, chain_id=999, legacy gasPrice
  - No gauge / no staking dance (prjx NFTs live in wallet directly)

Flow (every 4h cooldown when LP_AUTO_EXECUTE_PRJX_TOPUP=true):
  1. Read wallet balances (USDC + WHYPE + native HYPE for gas)
  2. Pick target position from managed_positions (chain=hyperevm, status=open)
  3. Cooldown check
  4. Compute deploy amount: min(MAX, total_balance * 0.98)
  5. Compute swap plan with shortage-only logic (#419 lesson)
  6. Get OpenOcean quote, do USDC → WHYPE swap if needed
  7. Wait + verify wallet balance
  8. Call increaseLiquidity with mins=1 (#419 lesson)
  9. Telegram alert, save cooldown

Standing directive: only LP wallet [[feedback-lp-dedicated-wallet]].

Shipped 2026-06-04 #414. Companion to #419 Aerodrome version.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.idle_deploy_dispatcher_prjx")


# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────

_REPO = Path(__file__).resolve().parents[3]
MANAGED_DB = _REPO / "engine" / "_registries" / "lp_managed_positions.db"
COOLDOWN_PATH = _REPO / "engine" / "_state" / "lp_idle_deploy_prjx_cooldown.json"
LOG_PATH = _REPO / "engine" / "_signals" / "lp_idle_deploy_prjx_audit.jsonl"
SUMMARY_PATH = _REPO / "engine" / "_state" / "lp_idle_deploy_prjx_latest.json"

# Tokens on HyperEVM
USDC_HYPER = "0xb88339CB7199b77E23DB6E890353E22632Ba630f"
WHYPE = "0x5555555555555555555555555555555555555555"
USDC_DECIMALS = 6
WHYPE_DECIMALS = 18

# Knobs (env-tunable)
AUTO_EXECUTE = os.environ.get("LP_AUTO_EXECUTE_PRJX_TOPUP", "false").lower() == "true"
MAX_DEPLOY_USD = float(os.environ.get("LP_PRJX_TOPUP_MAX_USD", "25"))  # conservative
MIN_DEPLOY_USD = float(os.environ.get("LP_PRJX_TOPUP_MIN_USD", "5"))
COOLDOWN_HOURS = float(os.environ.get("LP_PRJX_TOPUP_COOLDOWN_HOURS", "4"))
SWAP_SLIPPAGE_PCT = float(os.environ.get("LP_PRJX_SWAP_SLIPPAGE_PCT", "1.0"))
NATIVE_GAS_RESERVE_USD = float(os.environ.get("LP_PRJX_NATIVE_GAS_RESERVE_USD", "1.0"))
SWAP_DELTA_MIN_USD = 1.0  # don't swap for less than this (gas waste)


# ──────────────────────────────────────────────────────────────────────
# State helpers
# ──────────────────────────────────────────────────────────────────────

def _log(event: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    event["ts_iso"] = datetime.now(timezone.utc).isoformat()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(event, default=str) + "\n")


def _persist_summary(d: dict) -> None:
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_PATH.write_text(json.dumps(d, indent=2, default=str))


def _load_cooldown() -> dict:
    try:
        return json.loads(COOLDOWN_PATH.read_text())
    except Exception:
        return {}


def _save_cooldown(d: dict) -> None:
    COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOLDOWN_PATH.write_text(json.dumps(d, indent=2))


def _alert_telegram(text: str) -> None:
    try:
        from engine.telegram.client import send
        send("signal", key=f"prjx_idle_deploy:{int(time.time())}",
             text=text, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Telegram alert failed: %s", exc)


# ──────────────────────────────────────────────────────────────────────
# Wallet + position readers
# ──────────────────────────────────────────────────────────────────────

def _read_wallet_balance() -> dict:
    """Read USDC + WHYPE + native HYPE balances on HyperEVM."""
    from engine.strategies.lp_agile import env as _e
    from web3 import Web3

    cfg = _e.get_lp_config()
    wallet = Web3.to_checksum_address(cfg["wallet_address"])
    rpc = os.environ.get("HYPEREVM_RPC_URL", "https://rpc.hyperliquid.xyz/evm")
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))

    erc20 = [
        {"inputs": [{"name": "_o", "type": "address"}], "name": "balanceOf",
         "outputs": [{"name": "", "type": "uint256"}],
         "stateMutability": "view", "type": "function"},
    ]
    usdc_atomic = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_HYPER), abi=erc20,
    ).functions.balanceOf(wallet).call()
    whype_atomic = w3.eth.contract(
        address=Web3.to_checksum_address(WHYPE), abi=erc20,
    ).functions.balanceOf(wallet).call()
    native_atomic = w3.eth.get_balance(wallet)

    # Get approximate HYPE price for USD math
    hype_price = _hype_price_usd()
    return {
        "usdc_atomic": usdc_atomic,
        "usdc_usd": usdc_atomic / 10**USDC_DECIMALS,
        "whype_atomic": whype_atomic,
        "whype_usd": (whype_atomic / 10**WHYPE_DECIMALS) * hype_price,
        "native_atomic": native_atomic,
        "native_usd": (native_atomic / 10**18) * hype_price,
        "hype_price_usd": hype_price,
    }


def _hype_price_usd() -> float:
    """Get HYPE/USD price from chain snapshot or fallback."""
    try:
        snap_path = _REPO / "ops" / "pwa" / "serve" / "lp_agile_latest.json"
        d = json.loads(snap_path.read_text())
        for p in d.get("open_positions") or []:
            proto = (p.get("protocol") or "").lower()
            if "prjx" in proto and "WHYPE" in (p.get("pair") or "").upper():
                # value/(amount * 1.0) is a rough proxy
                amt = p.get("token0_amount") or p.get("amount0")
                val = p.get("value_usd")
                if amt and val and float(amt) > 0:
                    return float(val) / (float(amt) * 2)  # paired w/ stable
    except Exception:
        pass
    return float(os.environ.get("HYPE_PRICE_USD_FALLBACK", "40.0"))


def _find_target_position() -> Optional[dict]:
    """Pick target via shared multi-pool picker (#420).

    Below floor → fill biggest gap first. At floor → highest APR.
    Routes through pick_target_position which both dispatchers share.

    Note: still constrained to USDC/WHYPE pairs initially because the
    OpenOcean USDC↔WHYPE swap is the only path wired. WHYPE/UBTC etc.
    would need swap routing in idle_deploy_dispatcher_prjx swap_plan.
    For now we filter to WHYPE+USDC pairs at the dispatcher level.
    """
    try:
        from engine.strategies.lp_agile.idle_deploy_picker import (
            pick_target_position,
        )
        # Get all prjx pools; we still filter to WHYPE/USDC at the picker level
        # because our swap path is USDC↔WHYPE only for now.
        target = pick_target_position(chain="hyperevm", protocol="prjx")
        if target is None:
            return None
        # Until multi-token swap is wired, ONLY top up WHYPE/USDC pairs
        pair_upper = (target.get("pair") or "").upper()
        if "WHYPE" not in pair_upper or "USDC" not in pair_upper:
            logger.info(
                "[prjx idle-deploy] picker chose %s but swap path is USDC/WHYPE only",
                target.get("pair"),
            )
            # Fall back to first WHYPE/USDC pool
            conn = sqlite3.connect(str(MANAGED_DB))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT * FROM managed_positions
                   WHERE chain='hyperevm' AND protocol='prjx'
                     AND status='open'
                     AND current_nft_token_id IS NOT NULL
                     AND token0_symbol='WHYPE' AND token1_symbol='USDC'
                   ORDER BY lifetime_capital_in_usd DESC LIMIT 1"""
            ).fetchone()
            conn.close()
            return dict(row) if row else None
        # Re-fetch the registry row for full schema
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
        # Attach the picker's metadata for the audit log
        result["_picker_reason"] = target.get("reason")
        result["_picker_below_floor"] = target.get("below_floor")
        result["_picker_fill_gap_usd"] = target.get("fill_gap_usd")
        return result
    except Exception as exc:
        logger.warning("multi-pool target pick failed: %s", exc)
        return None


# ──────────────────────────────────────────────────────────────────────
# Main flow
# ──────────────────────────────────────────────────────────────────────

def propose() -> dict:
    """Build a deploy proposal — no execution. Returns the proposal dict."""
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
    summary["target"] = {
        "id": target["id"], "pair": pair, "nft_token_id": nft,
        "current_capital_usd": target.get("lifetime_capital_in_usd"),
    }

    # Native HYPE for gas — don't drain
    if balance["native_usd"] < NATIVE_GAS_RESERVE_USD:
        summary["blocked"] = (
            f"native_gas_low (${balance['native_usd']:.2f} < "
            f"${NATIVE_GAS_RESERVE_USD:.2f} reserve)"
        )
        return summary

    # Cooldown
    last = cooldown.get(str(nft), 0)
    if now_ts - last < COOLDOWN_HOURS * 3600:
        eta_min = (COOLDOWN_HOURS * 3600 - (now_ts - last)) / 60
        summary["blocked"] = f"cooldown ({eta_min:.0f}m until next deploy)"
        return summary

    # Total deployable: USDC + WHYPE (excluding native gas reserve)
    total_usd = balance["usdc_usd"] + balance["whype_usd"]
    if total_usd < MIN_DEPLOY_USD:
        summary["blocked"] = f"idle_below_min (${total_usd:.2f} < ${MIN_DEPLOY_USD})"
        return summary

    deploy_total = min(MAX_DEPLOY_USD, total_usd * 0.98)
    if deploy_total < MIN_DEPLOY_USD:
        summary["blocked"] = (
            f"deploy_after_buffer_below_min (${deploy_total:.2f} < ${MIN_DEPLOY_USD})"
        )
        return summary

    # 50/50 split target — shortage-only swap calc (#419 lesson)
    target_each_side_usd = deploy_total / 2.0
    usdc_short = max(0.0, target_each_side_usd - balance["usdc_usd"])
    whype_short = max(0.0, target_each_side_usd - balance["whype_usd"])

    swap_plan = None
    if whype_short > SWAP_DELTA_MIN_USD:
        # Need more WHYPE → swap USDC → WHYPE
        usdc_excess = max(0.0, balance["usdc_usd"] - target_each_side_usd)
        if usdc_excess >= whype_short:
            swap_plan = {
                "direction": "USDC->WHYPE",
                "in_token": USDC_HYPER,
                "out_token": WHYPE,
                "in_decimals": USDC_DECIMALS,
                "out_decimals": WHYPE_DECIMALS,
                "amount_decimal": f"{whype_short:.4f}",  # USD ≈ USDC
                "estimated_out_usd": whype_short,
            }
        else:
            # Scale deploy down to what excess can support
            achievable = balance["usdc_usd"] * 0.98 * 2.0
            deploy_total = min(deploy_total, achievable)
            target_each_side_usd = deploy_total / 2.0
            whype_short = max(0.0, target_each_side_usd - balance["whype_usd"])
            if whype_short > SWAP_DELTA_MIN_USD:
                swap_plan = {
                    "direction": "USDC->WHYPE",
                    "in_token": USDC_HYPER,
                    "out_token": WHYPE,
                    "in_decimals": USDC_DECIMALS,
                    "out_decimals": WHYPE_DECIMALS,
                    "amount_decimal": f"{whype_short:.4f}",
                    "estimated_out_usd": whype_short,
                }
    elif usdc_short > SWAP_DELTA_MIN_USD:
        whype_excess = max(0.0, balance["whype_usd"] - target_each_side_usd)
        if whype_excess >= usdc_short:
            # Swap WHYPE → USDC. amount_decimal is WHYPE units, not USD.
            whype_amount = usdc_short / balance["hype_price_usd"]
            swap_plan = {
                "direction": "WHYPE->USDC",
                "in_token": WHYPE,
                "out_token": USDC_HYPER,
                "in_decimals": WHYPE_DECIMALS,
                "out_decimals": USDC_DECIMALS,
                "amount_decimal": f"{whype_amount:.6f}",
                "estimated_out_usd": usdc_short,
            }
    # else: both sides sufficient → no swap, just deploy

    deploy_usdc_usd = target_each_side_usd
    deploy_whype_usd = target_each_side_usd
    deploy_usdc_atomic = int(deploy_usdc_usd * (10 ** USDC_DECIMALS))
    deploy_whype_atomic = int(
        (deploy_whype_usd / balance["hype_price_usd"]) * (10 ** WHYPE_DECIMALS)
    )

    summary["proposal"] = {
        "nft_token_id": nft,
        "pair": pair,
        "deploy_usd_total": round(deploy_total, 2),
        "pre_swap": swap_plan,
        "amount0_desired_atomic": deploy_whype_atomic,    # WHYPE = token0
        "amount1_desired_atomic": deploy_usdc_atomic,     # USDC = token1
        "amount0_desired_human": round(deploy_whype_usd / balance["hype_price_usd"], 6),
        "amount1_desired_human": round(deploy_usdc_usd, 4),
        "estimated_gas_usd": 0.10 if swap_plan else 0.05,
        "hype_price_usd": balance["hype_price_usd"],
    }
    return summary


def tick() -> dict:
    """One run of the dispatcher. Propose; execute when AUTO_EXECUTE=true."""
    started = time.time()
    summary = propose()
    summary["latency_ms"] = int((time.time() - started) * 1000)

    if not summary.get("proposal") or summary.get("blocked"):
        summary["mode"] = "blocked" if summary.get("blocked") else "no_proposal"
        _log({"action": "tick_blocked", **summary})
        _persist_summary(summary)
        return summary

    if not AUTO_EXECUTE:
        summary["mode"] = "dry_run_auto_disabled"
        _log({"action": "dry_run", **summary})
        _persist_summary(summary)
        return summary

    # ─── LIVE EXECUTION PATH ───────────────────────────────────────────
    p = summary["proposal"]

    # Step 1: Swap if needed
    if p.get("pre_swap"):
        try:
            from engine.strategies.lp_agile.swap_openocean import (
                get_openocean_quote, execute_openocean_swap,
            )
            sp = p["pre_swap"]
            from engine.strategies.lp_agile import env as _e
            quote = get_openocean_quote(
                in_token=sp["in_token"],
                out_token=sp["out_token"],
                amount_decimal=sp["amount_decimal"],
                slippage_pct=SWAP_SLIPPAGE_PCT,
                account=_e.get_lp_config()["wallet_address"],
                in_decimals=sp["in_decimals"],
                out_decimals=sp["out_decimals"],
            )
            swap_result = execute_openocean_swap(quote, dry_run=False)
            summary["swap_result"] = {
                "executed": swap_result.executed,
                "tx_hash": swap_result.tx_hash,
                "in_amount_atomic": swap_result.in_amount_atomic,
                "actual_out_atomic": swap_result.actual_out_atomic,
                "error": swap_result.error,
            }
            if not swap_result.executed:
                summary["mode"] = "swap_failed"
                _log({"action": "swap_failed", **summary})
                _persist_summary(summary)
                return summary
            # Wait for nonce settle (#419 lesson)
            time.sleep(15)
            # Refresh balance + recompute atomic amounts
            fresh = _read_wallet_balance()
            p["amount0_desired_atomic"] = min(
                p["amount0_desired_atomic"], int(fresh["whype_atomic"] * 0.99),
            )
            p["amount1_desired_atomic"] = min(
                p["amount1_desired_atomic"], int(fresh["usdc_atomic"] * 0.99),
            )
        except Exception as exc:
            summary["mode"] = "swap_exception"
            summary["swap_result"] = {"error": f"exception: {exc}"}
            _log({"action": "swap_exception", **summary})
            _persist_summary(summary)
            return summary

    # Step 2: increaseLiquidity
    try:
        from engine.strategies.lp_agile.executor import (
            sign_and_send_increase_liquidity_prjx,
        )
        result = sign_and_send_increase_liquidity_prjx(
            nft_token_id=int(p["nft_token_id"]),
            amount0_desired=int(p["amount0_desired_atomic"]),
            amount1_desired=int(p["amount1_desired_atomic"]),
            deadline_sec=600,
            dry_run=False,
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
                f"✅ <b>LP prjx idle-deploy fired</b>\n"
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


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    out = tick()
    print(json.dumps(out, indent=2, default=str))
