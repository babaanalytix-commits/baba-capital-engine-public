"""engine/strategies/lp_agile/lp_veto.py — 24h migration veto store + lifecycle.

A Base pool-migration recommendation isn't executed immediately — it opens a 24h
window during which the operator can stop it (PWA tap or Telegram button). Silence
= consent: once the window passes with no veto, it becomes eligible to execute
(and the candidate is RE-VALIDATED at that point by the caller). This module is
the durable record + the lifecycle rules. Pure given an injected `now` → testable.

Lifecycle:  awaiting_veto ──(operator stops)──▶ vetoed
                  │
                  └──(deadline passes, no veto)──▶ ready ──(executor fires)──▶ executed

Channels:
  • PWA writes vetoed ids to engine/_state/lp_veto_requests.json; ingest_pwa_vetoes()
    folds them in.
  • Telegram inline-button callback calls veto(id) directly.
Store: engine/_state/lp_migration_vetoes.json.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.lp_veto")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_STORE = _REPO_ROOT / "engine" / "_state" / "lp_migration_vetoes.json"
_PWA_REQUESTS = _REPO_ROOT / "engine" / "_state" / "lp_veto_requests.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(iso: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(iso)
    except Exception:
        return None


def _load(store: Optional[dict] = None) -> dict:
    if store is not None:
        return store
    try:
        return json.loads(_STORE.read_text())
    except Exception:
        return {}


def _save(d: dict, store: Optional[dict] = None) -> None:
    if store is not None:
        return
    try:
        _STORE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STORE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2, default=str))
        tmp.replace(_STORE)
    except Exception as exc:                                          # noqa: BLE001
        logger.warning("[lp_veto] save failed: %s", exc)


def open_veto(record: dict, *, store: Optional[dict] = None) -> dict:
    """Register a pending-veto record (idempotent on id). Record needs id, chain,
    pair, candidate, executes_after_iso."""
    d = _load(store)
    rid = record["id"]
    if rid not in d:
        d[rid] = {**record, "status": "awaiting_veto", "registered_iso": _now().isoformat()}
        _save(d, store)
        logger.info("[lp_veto] opened veto window %s (executes_after %s)",
                    rid, record.get("executes_after_iso"))
    return d[rid]


def veto(rid: str, *, by: str = "operator", store: Optional[dict] = None) -> bool:
    """Operator stops a pending migration. Returns True if it was awaiting."""
    d = _load(store)
    j = d.get(rid)
    if not j or j.get("status") != "awaiting_veto":
        return False
    j["status"] = "vetoed"
    j["vetoed_by"] = by
    j["vetoed_iso"] = _now().isoformat()
    _save(d, store)
    logger.info("[lp_veto] %s VETOED by %s", rid, by)
    return True


def is_ready(rid: str, *, now: Optional[datetime] = None, store: Optional[dict] = None) -> bool:
    d = _load(store)
    j = d.get(rid)
    if not j or j.get("status") != "awaiting_veto":
        return False
    dl = _parse(j.get("executes_after_iso") or "")
    return bool(dl and (now or _now()) >= dl)


def ready_to_execute(*, now: Optional[datetime] = None, store: Optional[dict] = None) -> list:
    d = _load(store)
    return [j for rid, j in d.items() if is_ready(rid, now=now, store=d if store is None else store)]


def mark_executed(rid: str, *, store: Optional[dict] = None) -> None:
    d = _load(store)
    if rid in d:
        d[rid]["status"] = "executed"
        d[rid]["executed_iso"] = _now().isoformat()
        _save(d, store)


def ingest_pwa_vetoes(*, store: Optional[dict] = None) -> int:
    """Fold PWA-written veto ids (engine/_state/lp_veto_requests.json) into the
    store. Returns count applied. The PWA appends ids; we apply + clear."""
    try:
        ids = json.loads(_PWA_REQUESTS.read_text())
        ids = ids if isinstance(ids, list) else ids.get("vetoed_ids", [])
    except Exception:
        return 0
    n = sum(1 for rid in ids if veto(rid, by="pwa", store=store))
    try:
        _PWA_REQUESTS.write_text(json.dumps([]))   # clear after applying
    except Exception:
        pass
    return n


def sync_from_board(pending_veto: list, *, store: Optional[dict] = None) -> int:
    """Register any new pending_veto records from the migration board."""
    return sum(1 for r in pending_veto if open_veto(r, store=store).get("status") == "awaiting_veto"
               and r["id"] not in _load(store))  # count is best-effort
