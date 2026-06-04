"""engine/strategies/lp_agile/lp_auto.py — single autonomous LP pass (dry-run default).

One entrypoint the job runner calls each cycle. It refreshes every LP board and
runs the (gated, default dry-run) action loops, honouring the master kill switch.
Nothing signs unless the operator has flipped LP_AUTO_EXECUTE + LP_CLOSE_LIVE and
funded the gas float — and even then only Base/Slipstream, only within guardrails.

Order: kill-switch check → report → rebalance(detect+state-machine) → income →
migration(+veto sync) → allocation → publish lp_auto_status.json.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from . import lp_guardrails as guard

logger = logging.getLogger("engine.strategies.lp_agile.lp_auto")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_STATUS = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_auto_status.json"


def _close_fn(dry_run: bool):
    from . import close_executor
    def fn(plan: dict) -> dict:
        r = close_executor.sign_and_send_close(
            chain=(plan.get("chain") or "base"),
            protocol=(plan.get("protocol") or "slipstream"),
            token_id=int(plan.get("nft_token_id") or 0),
            dry_run=dry_run)
        return {"ok": bool(r.success), "error": r.error, "tx_hashes": r.tx_hashes,
                "dry_run": r.dry_run}
    return fn


def _mint_fn_placeholder(plan: dict) -> dict:
    # Re-mint at the new range requires constructing an LPSignal from the plan's
    # new_range (range_optimizer + pool). That signer wire is the last live step;
    # in dry-run we report intent without fabricating a fake success.
    return {"ok": False, "error": "remint signer not wired (dry-run intent only)",
            "nft_token_id": None}


def run_autonomous_pass(*, dry_run: bool = True, write: bool = True) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    if guard.kill_switch_active():
        status = {"generated_at_iso": now, "halted": True,
                  "reason": "LP kill switch active — no actions taken"}
        if write:
            _publish(status)
        return status

    summary = {"generated_at_iso": now, "halted": False, "dry_run": dry_run, "steps": {}}

    # 1. Report (positions + venue APR + discovery)
    try:
        from . import lp_report
        rep = lp_report.publish() if hasattr(lp_report, "publish") else lp_report.build_lp_report()
        summary["steps"]["report"] = {"ok": True,
                                      "n_positions": rep.get("n_positions") if isinstance(rep, dict) else None}
    except Exception as exc:                                          # noqa: BLE001
        summary["steps"]["report"] = {"ok": False, "error": str(exc)[:160]}

    # 2. Rebalance — detect, then run each through the state machine (gated/dry-run)
    try:
        from . import lp_rebalancer, lp_rebalance_executor as X
        items = lp_rebalancer.plan_rebalances()
        cfn, mfn = _close_fn(dry_run), _mint_fn_placeholder
        jobs = []
        for it in items:
            plan = it["plan"]
            plan.setdefault("chain", it.get("chain"))
            jobs.append(X.execute_rebalance(plan, close_fn=cfn, mint_fn=mfn, dry_run=dry_run))
        lp_rebalancer.run_rebalance_cycle(write=True)   # publish the board too
        summary["steps"]["rebalance"] = {
            "ok": True, "n_flagged": len(items),
            "states": [j.get("state") for j in jobs],
            "recover_needed": [j["plan_id"] for j in jobs if j.get("recover_needed")]}
    except Exception as exc:                                          # noqa: BLE001
        summary["steps"]["rebalance"] = {"ok": False, "error": str(exc)[:160]}

    # 3. Income (compound/harvest)
    try:
        from . import lp_income
        inc = lp_income.run_income_cycle(write=True)
        summary["steps"]["income"] = {"ok": True,
                                      "harvestable_usd": inc.get("harvestable_income_usd"),
                                      "compounding_usd": inc.get("compounding_usd")}
    except Exception as exc:                                          # noqa: BLE001
        summary["steps"]["income"] = {"ok": False, "error": str(exc)[:160]}

    # 4. Migration evaluate + veto-store sync + readiness
    try:
        from . import lp_migration, lp_veto
        board = lp_migration.run(write=True, send_telegram=False)
        for rec in board.get("pending_veto", []):
            lp_veto.open_veto(rec)
        lp_veto.ingest_pwa_vetoes()
        ready = lp_veto.ready_to_execute()
        summary["steps"]["migration"] = {
            "ok": True, "n_recommend": board.get("n_recommend"),
            "n_veto_window": board.get("n_veto_window"),
            "n_ready_after_veto": len(ready)}
    except Exception as exc:                                          # noqa: BLE001
        summary["steps"]["migration"] = {"ok": False, "error": str(exc)[:160]}

    # 5. Allocation plan
    try:
        from . import lp_allocation_planner
        plan = lp_allocation_planner.run(write=True)
        summary["steps"]["allocation"] = {
            "ok": True, "plans": [{"chain": p["chain"], "n_current": p["n_current"],
                                   "n_target": p["n_target"], "status": p["status"]}
                                  for p in plan.get("plans", [])]}
    except Exception as exc:                                          # noqa: BLE001
        summary["steps"]["allocation"] = {"ok": False, "error": str(exc)[:160]}

    # 6. TA range view (advisory both chains; Base auto-candidate flag only)
    try:
        from . import lp_ta_board
        tb = lp_ta_board.run(write=True)
        summary["steps"]["ta_view"] = {
            "ok": True,
            "holds": [r["pair"] for r in tb.get("positions", []) if r.get("action") == "hold"],
            "auto_candidates": [r["pair"] for r in tb.get("positions", []) if r.get("mode") == "auto_candidate"]}
    except Exception as exc:                                          # noqa: BLE001
        summary["steps"]["ta_view"] = {"ok": False, "error": str(exc)[:160]}

    if write:
        _publish(summary)
    return summary


def _publish(status: dict) -> None:
    try:
        _STATUS.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATUS.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, indent=2, default=str))
        tmp.replace(_STATUS)
        logger.info("[lp_auto] status → %s", _STATUS)
    except Exception as exc:                                          # noqa: BLE001
        logger.error("[lp_auto] publish failed: %s", exc)


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="One autonomous LP pass (dry-run default)")
    p.add_argument("--live", action="store_true", help="allow gated live execution")
    p.add_argument("--no-write", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(json.dumps(run_autonomous_pass(dry_run=not args.live, write=not args.no_write),
                     indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
