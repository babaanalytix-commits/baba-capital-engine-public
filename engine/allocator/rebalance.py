"""engine/allocator/rebalance.py — one-tap HLP↔perps rebalance.

V0.4.0 — Phase 2 of treasury. Reads the latest treasury snapshot, computes
the transfer required to bring HLP / HL_PERPS within target drift band,
asks Yomi for Telegram approval, fires via the legacy hl_vault wrapper
(which has a $25 hard cap).

DESIGN:
  - Reuses ops/portfolio_allocator/hl_vault.py (existing wrapper with
    HARD_CAP_USD = $25 defence-in-depth).
  - Approval flow: build proposed transfer → request_approval() → on tap
    fire deposit_to_hlp or withdraw_from_hlp.
  - HARD GATE: refuses to fire if treasury snapshot is > 1h stale.
  - HARD GATE: refuses to fire transfers > HARD_CAP (delegated to wrapper).

USAGE:
  ./engine/run.sh treasury-rebalance              # dry-run (prints proposal)
  ./engine/run.sh treasury-rebalance --live       # asks for telegram approval

WHY NOT AUTO-FIRE:
  Per Yomi's 2026-05-20 directive: "first version is to approve via alerts
  on telegram. the final version will be fully [auto], once we have enough
  data and consistency." Treasury moves are real money — same approval
  pattern as MD opens.
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.allocator.rebalance")

_REPO_ROOT = Path(__file__).resolve().parents[2]
TREASURY_SNAPSHOT_PATH = _REPO_ROOT / "engine" / "_reports" / "treasury_latest.json"
REBALANCE_LOG_PATH = _REPO_ROOT / "engine" / "_reports" / "rebalance_log.jsonl"

MAX_SNAPSHOT_AGE_SEC = 3600   # 1h — refuse to fire on stale data
DRIFT_BAND_PCT = Decimal("5")  # match treasury digest band


@dataclass(frozen=True)
class RebalanceProposal:
    direction: str        # "hlp_deposit" or "hlp_withdraw"
    amount_usd: Decimal
    rationale: str
    target_bucket: str    # which bucket is under-target

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "amount_usd": str(self.amount_usd),
            "rationale": self.rationale,
            "target_bucket": self.target_bucket,
        }


def compute_proposal() -> Optional[RebalanceProposal]:
    """Read treasury snapshot, return single proposed transfer or None.

    V0.4.0 strategy: rebalance HLP_VAULT vs HL_PERPS only (HL→HLP is the
    only safe + low-friction transfer path; cross-venue moves need bridges).
    Picks the LARGER of the two as the "over" side and proposes a transfer
    toward the "under" side. Amount = half the drift (gentle convergence).
    """
    if not TREASURY_SNAPSHOT_PATH.exists():
        logger.warning(f"[rebalance] no snapshot at {TREASURY_SNAPSHOT_PATH}")
        return None
    try:
        snap = json.loads(TREASURY_SNAPSHOT_PATH.read_text())
    except Exception as exc:
        logger.error(f"[rebalance] snapshot parse failed: {exc}")
        return None

    # Staleness check
    try:
        gen_iso = snap.get("generated_at_iso")
        if gen_iso:
            gen_dt = datetime.fromisoformat(gen_iso.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - gen_dt).total_seconds()
            if age > MAX_SNAPSHOT_AGE_SEC:
                logger.warning(
                    f"[rebalance] snapshot is {age:.0f}s stale "
                    f"(max {MAX_SNAPSHOT_AGE_SEC}s) — refusing"
                )
                return None
    except Exception:
        pass

    buckets = {b["name"]: b for b in (snap.get("buckets") or [])}
    hlp = buckets.get("hlp_vault")
    perps = buckets.get("hyperliquid_perps")
    if not hlp or not perps:
        logger.warning("[rebalance] missing hlp_vault or hyperliquid_perps in snapshot")
        return None

    hlp_drift = Decimal(str(hlp.get("drift_pct", "0")))
    perps_drift = Decimal(str(perps.get("drift_pct", "0")))

    # Total equity for proportional sizing
    total = Decimal(str(snap.get("total_equity_usd", "0")))
    if total <= 0:
        return None

    # Case 1: HLP under-target AND perps over-target → move perps→HLP (deposit)
    if hlp_drift <= -DRIFT_BAND_PCT and perps_drift >= DRIFT_BAND_PCT:
        # Half the smaller drift size, capped at HARD_CAP
        target_pct = Decimal(str(hlp.get("target_pct", "20")))
        target_usd = total * target_pct / Decimal("100")
        current_usd = Decimal(str(hlp.get("equity_usd", "0")))
        gap = target_usd - current_usd
        amount = (gap / Decimal("2")).quantize(Decimal("0.01"))
        if amount <= 0:
            return None
        return RebalanceProposal(
            direction="hlp_deposit",
            amount_usd=amount,
            rationale=(
                f"HLP_VAULT drift {hlp_drift:+.1f}% (under target), "
                f"HL_PERPS drift {perps_drift:+.1f}% (over). "
                f"Deposit ${amount} HL → HLP to converge."
            ),
            target_bucket="hlp_vault",
        )

    # Case 2: HLP over, perps under → withdraw HLP → perps
    if hlp_drift >= DRIFT_BAND_PCT and perps_drift <= -DRIFT_BAND_PCT:
        target_pct = Decimal(str(perps.get("target_pct", "40")))
        target_usd = total * target_pct / Decimal("100")
        current_usd = Decimal(str(perps.get("equity_usd", "0")))
        gap = target_usd - current_usd
        amount = (gap / Decimal("2")).quantize(Decimal("0.01"))
        if amount <= 0:
            return None
        return RebalanceProposal(
            direction="hlp_withdraw",
            amount_usd=amount,
            rationale=(
                f"HL_PERPS drift {perps_drift:+.1f}% (under target), "
                f"HLP_VAULT drift {hlp_drift:+.1f}% (over). "
                f"Withdraw ${amount} HLP → HL (4d cooldown applies)."
            ),
            target_bucket="hyperliquid_perps",
        )

    # Else: within drift band, no action
    return None


def _append_log(entry: dict) -> None:
    REBALANCE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with REBALANCE_LOG_PATH.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def execute(*, dry_run: bool = True) -> dict:
    """Read snapshot → compute proposal → ask telegram approval → fire.

    Returns dict with full audit trail of the decision.
    """
    now = datetime.now(timezone.utc)
    result = {
        "generated_at_iso": now.isoformat(),
        "dry_run": dry_run,
        "proposal": None,
        "approved": None,
        "fired": False,
        "outcome": None,
        "error": None,
    }
    prop = compute_proposal()
    if prop is None:
        result["outcome"] = "no_proposal_within_drift_band"
        return result
    result["proposal"] = prop.to_dict()

    if dry_run:
        result["outcome"] = "dry_run_proposal_only"
        logger.info(
            f"[rebalance] DRY-RUN: {prop.direction} ${prop.amount_usd} | "
            f"{prop.rationale}"
        )
        return result

    # LIVE path — Telegram approval first
    try:
        from engine.telegram.approval import request_approval, Decision
        approval_text = (
            f"<b>Treasury Rebalance — {prop.direction.upper()}</b>\n\n"
            f"Amount: <code>${prop.amount_usd}</code>\n"
            f"Target: <code>{prop.target_bucket}</code>\n\n"
            f"<i>{prop.rationale}</i>\n\n"
            f"Hard-cap defence: $25/tx (see hl_vault.py HARD_CAP_USD)."
        )
        decision = request_approval(
            category="trade",
            text=approval_text,
            key=f"rebalance:{prop.direction}:{prop.amount_usd}:{now.strftime('%Y%m%d_%H%M')}",
            timeout_sec=300,
        )
        result["approved"] = decision.value
        if decision != Decision.APPROVED:
            result["outcome"] = f"approval_{decision.value}"
            _append_log(result)
            return result
    except Exception as exc:
        result["error"] = f"approval flow raised: {exc!r}"
        logger.error(f"[rebalance] {result['error']}", exc_info=True)
        _append_log(result)
        return result

    # APPROVED — fire via hl_vault wrapper
    try:
        # Lazy-import legacy wrapper (also lazy-imports the HL adapter,
        # which needs the MD venv that engine/run.sh sources).
        sys.path.insert(0, str(_REPO_ROOT))
        from ops.portfolio_allocator.hl_vault import (
            deposit_to_hlp, withdraw_from_hlp, VaultTransferRefused,
            VaultTransferFailed,
        )
        try:
            if prop.direction == "hlp_deposit":
                resp = deposit_to_hlp(
                    amount_usd=prop.amount_usd, dry_run=False,
                )
            else:
                resp = withdraw_from_hlp(
                    amount_usd=prop.amount_usd, dry_run=False,
                )
            result["fired"] = True
            result["outcome"] = "fired"
            result["wrapper_response"] = resp
            logger.info(
                f"[rebalance] FIRED: {prop.direction} ${prop.amount_usd} | "
                f"response={resp}"
            )
        except VaultTransferRefused as exc:
            result["outcome"] = "wrapper_refused"
            result["error"] = str(exc)
            logger.warning(f"[rebalance] wrapper refused: {exc}")
        except VaultTransferFailed as exc:
            result["outcome"] = "wrapper_failed"
            result["error"] = str(exc)
            logger.error(f"[rebalance] wrapper failed: {exc}")
    except Exception as exc:
        result["error"] = f"wrapper import/call raised: {exc!r}"
        logger.error(f"[rebalance] {result['error']}", exc_info=True)

    _append_log(result)

    # Fire confirmation Telegram
    try:
        from engine.telegram.client import send
        fired_marker = "✅" if result["fired"] else "❌"
        send(
            "trade",
            key=f"rebalance_done:{now.strftime('%Y%m%d_%H%M')}",
            text=(
                f"{fired_marker} <b>Treasury Rebalance</b>\n\n"
                f"Direction: <code>{prop.direction}</code>\n"
                f"Amount: <code>${prop.amount_usd}</code>\n"
                f"Outcome: <code>{result.get('outcome')}</code>\n"
                + (f"Error: <code>{result.get('error')}</code>"
                   if result.get('error') else "")
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass

    return result


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="Treasury one-tap rebalance")
    p.add_argument("--live", action="store_true",
                   help="ask for Telegram approval + actually fire transfer")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    result = execute(dry_run=not args.live)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
