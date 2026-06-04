"""engine/strategies/lp_agile/lp_migration.py — LP-REVAMP P4b: fee-aware pool-migration evaluator.

Answers one question per open position: "should this capital move to a better
pool, AFTER paying to get there?" It never chases headline APR — it nets out the
full cost of moving (gas round-trip + swap slippage + realizing current IL) and
only recommends a move that clears a margin AND pays that cost back inside a
sensible window.

Per chain (Yomi's locked policy):
  • base      → recommendation opens a 24h VETO window (PWA + Telegram). Silence =
                proceed; the candidate is RE-VALIDATED at execution time. Auto-
                execution itself is gated by lp_guardrails (+ gas reserve) and the
                close-side signer — until those exist it stays notify-only.
  • hyperevm  → ADVISORY only. prjx mints via an OpenOcean zap we don't sign yet,
                so HyperEVM moves are surfaced for the operator to do by hand.

Source of truth: ops/pwa/serve/lp_report.json (held positions w/ venue APR + IL)
and its DeFiLlama discovery board (safe same-chain candidates). Pure given that
input → unit-testable. Publishes ops/pwa/serve/lp_migration_board.json.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.lp_migration")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REPORT = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_report.json"
BOARD_OUT = _REPO_ROOT / "ops" / "pwa" / "serve" / "lp_migration_board.json"

_ADVISORY_CHAINS = {"hyperevm"}            # surface only — no signer
_VETO_CHAINS = {"base"}                    # 24h veto then (future) auto-execute
_GAS_ROUND_TRIP_USD = {"base": 3.0, "hyperevm": 0.5}


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _norm(s: str) -> frozenset:
    import re
    return frozenset(p for p in re.split(r"[-/ +]", (s or "").upper()) if p)


def evaluate(report: dict) -> list[dict]:
    """Pure: for each held position, decide hold vs recommend-migrate. Returns a
    list of decision dicts (one per position)."""
    min_margin = _f("LP_MIGRATION_MIN_MARGIN_PP", 5.0)
    max_payback = _f("LP_MIGRATION_MAX_PAYBACK_DAYS", 30.0)
    swap_slip = _f("LP_MIGRATION_SWAP_SLIPPAGE_PCT", 0.3) / 100.0

    positions = report.get("positions") or []
    disc = ((report.get("discovery") or {}).get("top_all_chains")) or []
    held = {_norm(p.get("pair")) for p in positions}     # never recommend a pool we already hold
    out = []
    for p in positions:
        chain = (p.get("chain") or "").lower()
        pair = p.get("pair")
        value = float(p.get("value_usd") or 0)
        cur_apr = p.get("venue_apy_pct")
        il = max(0.0, float(p.get("il_unrealised_usd") or 0))   # realized on exit

        # same-chain, safe, executable candidates that aren't a pool we ALREADY hold
        cands = [c for c in disc
                 if (c.get("chain") or "").lower() in _chain_aliases(chain)
                 and _norm(c.get("symbol")) not in held
                 and c.get("apy") is not None]
        # rank by IL-HAIRCUT apr — a volatile-pair (ilRisk=yes) headline overstates
        # realized return, so discount it before choosing the "best" candidate.
        def _eff_apr(c):
            a = float(c.get("apy") or 0)
            return a * 0.6 if str(c.get("il_risk") or "").lower() == "yes" else a
        best = max(cands, key=_eff_apr, default=None)

        gas = _GAS_ROUND_TRIP_USD.get(chain, 3.0)
        rec = {
            "chain": chain, "pair": pair, "value_usd": round(value, 2),
            "current_apr_pct": round(cur_apr, 2) if isinstance(cur_apr, (int, float)) else None,
            "mode": "advisory" if chain in _ADVISORY_CHAINS else (
                "veto_24h" if chain in _VETO_CHAINS else "advisory"),
        }

        if cur_apr is None or best is None or value <= 0:
            rec.update({"decision": "hold",
                        "reason": "no venue APR or no safe same-chain candidate to compare"})
            out.append(rec)
            continue

        cand_apr = float(best.get("apy") or 0)
        cand_il_risk = str(best.get("il_risk") or "").lower() == "yes"
        eff_cand_apr = cand_apr * 0.6 if cand_il_risk else cand_apr   # IL-haircut for the bar
        improvement_pp = round(eff_cand_apr - float(cur_apr), 2)
        # never AUTO-migrate into a high-IL (volatile/volatile) pool — surface it
        # for a human instead, even on Base.
        if cand_il_risk and rec["mode"] == "veto_24h":
            rec["mode"] = "advisory"
            rec["auto_downgraded"] = "candidate is high-IL (volatile pair) — advisory only, no auto-move"
        migration_cost = round(gas + swap_slip * value + il, 2)
        annual_gain = (improvement_pp / 100.0) * value
        payback_days = round(migration_cost / (annual_gain / 365.0), 1) if annual_gain > 0 else None

        rec.update({
            "candidate": {"pair": best.get("symbol"), "project": best.get("project"),
                          "apy_pct": round(cand_apr, 2),
                          "il_haircut_apy_pct": round(eff_cand_apr, 2),
                          "il_risk": best.get("il_risk"), "tvl_usd": best.get("tvl_usd"),
                          "executable_now": best.get("executable_now")},
            "improvement_pp": improvement_pp,
            "migration_cost_usd": migration_cost,
            "cost_breakdown": {"gas": gas, "swap_slippage": round(swap_slip * value, 2),
                               "il_realized": round(il, 2)},
            "payback_days": payback_days,
        })

        worth = (improvement_pp >= min_margin and payback_days is not None
                 and payback_days <= max_payback and best.get("executable_now"))
        if worth:
            rec["decision"] = "recommend_migrate"
            rec["reason"] = (f"{best.get('symbol')} ({best.get('project')}) yields "
                             f"{cand_apr:.0f}% vs {float(cur_apr):.0f}% (+{improvement_pp:.1f}pp); "
                             f"move costs ${migration_cost:.2f}, repaid in ~{payback_days:.0f}d")
        else:
            rec["decision"] = "hold"
            if improvement_pp < min_margin:
                rec["reason"] = (f"best alt +{improvement_pp:.1f}pp < {min_margin:.0f}pp "
                                 f"margin — not worth the move")
            elif payback_days is None or payback_days > max_payback:
                rec["reason"] = (f"payback ~{payback_days}d > {max_payback:.0f}d cap "
                                 f"(cost ${migration_cost:.2f}) — not worth it yet")
            else:
                rec["reason"] = "best candidate not executable on this chain"
        out.append(rec)
    return out


def _chain_aliases(chain: str) -> set:
    if chain == "hyperevm":
        return {"hyperevm", "hyperliquid", "hyperliquid l1", "hyperliquidl1"}
    return {chain}


def _notification(rec: dict) -> dict:
    pair, chain = rec.get("pair"), rec.get("chain")
    if rec["decision"] != "recommend_migrate":
        return {"chain": chain, "pair": pair, "severity": "low",
                "event": f"{pair} ({chain}): staying put.",
                "action": rec.get("reason", ""), "mode": rec["mode"]}
    c = rec["candidate"]
    if rec["mode"] == "veto_24h":
        action = (f"Auto-migrating to {c['pair']} in 24h unless you stop it "
                  f"(PWA/Telegram). Candidate re-checked before the move.")
    else:
        action = (f"Consider moving to {c['pair']} ({c['project']}) yourself — "
                  f"+{rec['improvement_pp']:.1f}pp, repays in ~{rec['payback_days']:.0f}d.")
    return {
        "chain": chain, "pair": pair, "severity": "medium",
        "event": (f"Better pool for {pair}: {c['pair']} at {c['apy_pct']:.0f}% "
                  f"vs your {rec['current_apr_pct']:.0f}%."),
        "action": action,
        "cost": f"move ~${rec['migration_cost_usd']:.2f}, payback ~{rec['payback_days']:.0f}d",
        "mode": rec["mode"],
    }


def run(*, write: bool = True, send_telegram: bool = False) -> dict:
    try:
        report = json.loads(_REPORT.read_text())
    except Exception as exc:                                          # noqa: BLE001
        logger.warning("[lp_migration] no lp_report.json (%s)", exc)
        report = {"positions": [], "discovery": {}}

    decisions = evaluate(report)
    recs = [d for d in decisions if d["decision"] == "recommend_migrate"]
    notes = [_notification(d) for d in decisions]

    # Base recommendations open a 24h veto window (record only — execution is gated
    # by the close-side signer + guardrails, not yet wired).
    now = datetime.now(timezone.utc)
    veto = []
    for d in recs:
        if d["mode"] == "veto_24h":
            veto.append({
                "id": f"mig:{d['chain']}:{d['pair']}:{int(now.timestamp())}",
                "chain": d["chain"], "pair": d["pair"],
                "candidate": d["candidate"]["pair"],
                "opened_iso": now.isoformat(),
                "executes_after_iso": (now + timedelta(hours=24)).isoformat(),
                "status": "awaiting_veto",
            })

    board = {
        "generated_at_iso": now.isoformat(),
        "n_positions": len(decisions),
        "n_recommend": len(recs),
        "n_advisory": sum(1 for d in recs if d["mode"] == "advisory"),
        "n_veto_window": len(veto),
        "decisions": decisions,
        "notifications": notes,
        "pending_veto": veto,
    }
    if write:
        try:
            BOARD_OUT.parent.mkdir(parents=True, exist_ok=True)
            tmp = BOARD_OUT.with_suffix(".tmp")
            tmp.write_text(json.dumps(board, indent=2, default=str))
            tmp.replace(BOARD_OUT)
            logger.info("[lp_migration] %d positions, %d recommend (%d veto) → %s",
                        len(decisions), len(recs), len(veto), BOARD_OUT)
        except Exception as exc:                                      # noqa: BLE001
            logger.error("[lp_migration] board write failed: %s", exc)

    if send_telegram and recs:
        _alert_telegram(recs)
    return board


def _alert_telegram(recs: list) -> None:
    """Best-effort Telegram alert for migration recommendations (operator-side)."""
    try:
        from engine.telegram.client import send_message  # type: ignore
    except Exception:
        logger.debug("[lp_migration] telegram client unavailable — skipping alert")
        return
    for d in recs:
        n = _notification(d)
        txt = f"🔄 <b>LP pool move — {n['chain']}</b>\n{n['event']}\n{n['action']}\n{n.get('cost','')}"
        try:
            send_message(txt)
        except Exception as exc:                                      # noqa: BLE001
            logger.debug("[lp_migration] telegram send failed: %s", exc)


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Fee-aware LP pool-migration evaluator")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--telegram", action="store_true", help="send Telegram alerts")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(json.dumps(run(write=not args.no_write, send_telegram=args.telegram),
                     indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
