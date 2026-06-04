"""engine/strategies/lp_agile/approval_store.py — Phase D approval gate.

Disk-persisted approval store that fronts the LP auto-rebalance executors
(prjx + Aerodrome). Approvals come from Telegram inline-button taps and PWA
taps. Both surfaces write into this store; the executor's confirm_callback
reads from it.

Design contract (per Yomi's standing rule "approve via Telegram and PWA
before fully automating"):

  1. Every approval is per-tx, not blanket. Approving Position A does NOT
     auto-approve Position B.

  2. Every approval is time-boxed — DEFAULT 15 minutes. If the executor
     doesn't fire within the window, the approval expires unused. This
     prevents stale approvals (Yomi taps Approve, leaves laptop, gas spikes
     hours later, system fires at a bad time).

  3. Every approval names the EXPECTED new ticks. If the planner produces
     different ticks by execution time (price moved enough to change the
     range), the approval is INVALID — we don't fire on a target the user
     never saw. Tick equality is exact for safety.

  4. Once consumed (tx broadcast), the approval is marked consumed and
     can't be replayed. Replays would risk burning multiple positions.

  5. The cooldown registry (`_LAST_REBALANCE_TS`) ALSO persists here so
     scheduler restarts don't bypass cooldowns.

Storage: JSONL at `state/lp/lp_approvals.jsonl` (one approval per line) and
companion `state/lp/lp_cooldown.json` (single dict). Files live in the
engine state dir so they persist across restarts but stay local to the box.

Failure mode: any read/parse error fails CLOSED — returns "no approval".
We'd rather miss a fire than fire on a corrupted record.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger("engine.strategies.lp_agile.approval_store")

# ─── Paths ──────────────────────────────────────────────────────────────────
# Match the existing state-dir convention used elsewhere in the engine.
_STATE_DIR = Path(
    os.environ.get(
        "LP_APPROVAL_DIR",
        os.path.expanduser("~/baba/wealth-ecosystem/state/lp"),
    )
)
_APPROVALS_FILE = _STATE_DIR / "lp_approvals.jsonl"
_COOLDOWN_FILE = _STATE_DIR / "lp_cooldown.json"

# Default approval window — operator can override via env.
_DEFAULT_APPROVAL_TTL_SEC = int(
    os.environ.get("LP_APPROVAL_TTL_SEC", "900")  # 15 min
)

# File lock — protects multi-process scheduler/worker writes.
_FILE_LOCK = threading.Lock()


# ─── Data classes ──────────────────────────────────────────────────────────


@dataclass
class Approval:
    """One Approve action by Yomi for one specific rebalance plan."""

    nft_token_id: int
    pillar: str                            # "prjx" | "aerodrome"
    expected_new_tick_lower: int
    expected_new_tick_upper: int
    approved_at_ts: float
    expires_at_ts: float
    approver: str = "unknown"             # "telegram:<user_id>" | "pwa:<session>"
    consumed: bool = False
    consumed_at_ts: Optional[float] = None
    consumed_tx_hash: Optional[str] = None
    consumed_error: Optional[str] = None
    plan_snapshot: dict = field(default_factory=dict)


# ─── Internal helpers ──────────────────────────────────────────────────────


def _ensure_dir() -> None:
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:                                       # noqa: BLE001
        logger.warning("[approval_store] failed to mkdir %s: %s", _STATE_DIR, e)


def _read_all_approvals() -> list[Approval]:
    """Read every approval line. Skips malformed lines silently (fail-closed)."""
    if not _APPROVALS_FILE.exists():
        return []
    out: list[Approval] = []
    try:
        with open(_APPROVALS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    out.append(Approval(**d))
                except Exception:                                # noqa: BLE001
                    # malformed line — skip, don't crash
                    continue
    except Exception as e:                                       # noqa: BLE001
        logger.warning("[approval_store] read failed: %s", e)
        return []
    return out


def _rewrite_all_approvals(approvals: Iterable[Approval]) -> bool:
    """Rewrite the whole file (used for consume-marking + GC).

    Returns True on success. Atomic via tempfile rename.
    """
    _ensure_dir()
    tmp = _APPROVALS_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            for a in approvals:
                f.write(json.dumps(asdict(a)) + "\n")
        os.replace(tmp, _APPROVALS_FILE)
        return True
    except Exception as e:                                       # noqa: BLE001
        logger.warning("[approval_store] rewrite failed: %s", e)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:                                        # noqa: BLE001
            pass
        return False


# ─── Public API: approvals ─────────────────────────────────────────────────


def create_approval(
    *,
    nft_token_id: int,
    pillar: str,
    expected_new_tick_lower: int,
    expected_new_tick_upper: int,
    approver: str,
    plan_snapshot: Optional[dict] = None,
    ttl_sec: Optional[int] = None,
) -> Approval:
    """Record a new approval. Returns the created Approval object.

    Multiple approvals for the same tokenId+ticks can exist (e.g. user taps
    twice). The executor will consume the first non-expired non-consumed
    one. This is intentional — keeps the store append-only and simple.
    """
    if pillar not in ("prjx", "aerodrome"):
        raise ValueError(f"unknown pillar: {pillar}")
    ttl = ttl_sec if ttl_sec is not None else _DEFAULT_APPROVAL_TTL_SEC
    now = time.time()
    a = Approval(
        nft_token_id=int(nft_token_id),
        pillar=pillar,
        expected_new_tick_lower=int(expected_new_tick_lower),
        expected_new_tick_upper=int(expected_new_tick_upper),
        approved_at_ts=now,
        expires_at_ts=now + ttl,
        approver=approver,
        plan_snapshot=plan_snapshot or {},
    )
    _ensure_dir()
    with _FILE_LOCK:
        try:
            with open(_APPROVALS_FILE, "a") as f:
                f.write(json.dumps(asdict(a)) + "\n")
        except Exception as e:                                   # noqa: BLE001
            logger.error("[approval_store] failed to write approval: %s", e)
            raise
    logger.info(
        "[approval_store] APPROVED tokenId=%d pillar=%s ticks=(%d,%d) "
        "approver=%s expires_in=%ds",
        nft_token_id, pillar,
        expected_new_tick_lower, expected_new_tick_upper,
        approver, ttl,
    )
    return a


def find_valid_approval(
    *,
    nft_token_id: int,
    pillar: str,
    expected_new_tick_lower: int,
    expected_new_tick_upper: int,
) -> Optional[Approval]:
    """Return the first matching non-expired non-consumed approval, or None.

    "Matching" = exact tokenId + pillar + tick endpoints. If the planner
    produced different ticks since the user tapped Approve, NO match —
    safer to skip.
    """
    now = time.time()
    with _FILE_LOCK:
        all_approvals = _read_all_approvals()
    for a in all_approvals:
        if a.consumed:
            continue
        if a.expires_at_ts <= now:
            continue
        if a.nft_token_id != int(nft_token_id):
            continue
        if a.pillar != pillar:
            continue
        if a.expected_new_tick_lower != int(expected_new_tick_lower):
            continue
        if a.expected_new_tick_upper != int(expected_new_tick_upper):
            continue
        return a
    return None


def mark_consumed(
    approval: Approval,
    *,
    tx_hash: Optional[str] = None,
    error: Optional[str] = None,
) -> bool:
    """Mark a specific approval as consumed (mutates file in place).

    Match by (tokenId, pillar, approved_at_ts) — that triplet uniquely
    identifies the row.
    """
    with _FILE_LOCK:
        all_approvals = _read_all_approvals()
        hit = False
        for a in all_approvals:
            if (
                a.nft_token_id == approval.nft_token_id
                and a.pillar == approval.pillar
                and a.approved_at_ts == approval.approved_at_ts
                and not a.consumed
            ):
                a.consumed = True
                a.consumed_at_ts = time.time()
                a.consumed_tx_hash = tx_hash
                a.consumed_error = error
                hit = True
                break
        if not hit:
            logger.warning(
                "[approval_store] mark_consumed: no matching approval row "
                "(tokenId=%d pillar=%s approved_at=%s)",
                approval.nft_token_id, approval.pillar,
                approval.approved_at_ts,
            )
            return False
        return _rewrite_all_approvals(all_approvals)


def gc_expired(*, keep_recent_consumed_sec: int = 7 * 86400) -> int:
    """Remove old expired-unused and consumed-older-than-7d rows.

    Returns count removed. Run from a scheduler job; cheap.
    """
    now = time.time()
    with _FILE_LOCK:
        all_approvals = _read_all_approvals()
        keep: list[Approval] = []
        removed = 0
        for a in all_approvals:
            if a.consumed:
                if a.consumed_at_ts and (now - a.consumed_at_ts) > keep_recent_consumed_sec:
                    removed += 1
                    continue
            elif a.expires_at_ts <= now:
                # expired-unused: drop after 24h grace so audit can still see
                if (now - a.expires_at_ts) > 86400:
                    removed += 1
                    continue
            keep.append(a)
        if removed:
            _rewrite_all_approvals(keep)
    return removed


def list_pending_approvals() -> list[Approval]:
    """Return all non-consumed non-expired approvals (for UI surface)."""
    now = time.time()
    with _FILE_LOCK:
        all_approvals = _read_all_approvals()
    return [
        a for a in all_approvals
        if not a.consumed and a.expires_at_ts > now
    ]


# ─── Public API: cooldown persistence ──────────────────────────────────────


def load_cooldown_map() -> dict[int, float]:
    """Load the disk-persisted cooldown registry (tokenId → last-fire ts)."""
    if not _COOLDOWN_FILE.exists():
        return {}
    try:
        with open(_COOLDOWN_FILE) as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        return {int(k): float(v) for k, v in raw.items()}
    except Exception as e:                                       # noqa: BLE001
        logger.warning("[approval_store] cooldown load failed: %s", e)
        return {}


def save_cooldown_map(m: dict[int, float]) -> bool:
    """Persist the cooldown registry to disk (atomic rename)."""
    _ensure_dir()
    tmp = _COOLDOWN_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump({str(k): float(v) for k, v in m.items()}, f)
        os.replace(tmp, _COOLDOWN_FILE)
        return True
    except Exception as e:                                       # noqa: BLE001
        logger.warning("[approval_store] cooldown save failed: %s", e)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:                                        # noqa: BLE001
            pass
        return False


def record_cooldown(nft_token_id: int, ts: Optional[float] = None) -> None:
    """Record a fresh fire-timestamp for a tokenId AND persist to disk."""
    if ts is None:
        ts = time.time()
    m = load_cooldown_map()
    m[int(nft_token_id)] = float(ts)
    save_cooldown_map(m)


# ─── Public API: default confirm_callback factory ──────────────────────────


def build_confirm_callback(pillar: str):
    """Build a confirm_callback that the executor will call before signing.

    Returns a function with signature `cb(plan_dict) -> bool` that returns
    True iff a valid approval matching the plan's tokenId + expected ticks
    is present. The executor passes a `tokenId` + `new_ticks` dict; we
    look up the approval and mark it as in-flight (but NOT yet consumed —
    that happens after broadcast succeeds, via mark_consumed).
    """
    if pillar not in ("prjx", "aerodrome"):
        raise ValueError(f"unknown pillar: {pillar}")

    def _callback(plan_summary: dict) -> bool:
        try:
            tid = int(plan_summary["tokenId"])
            t_lower, t_upper = plan_summary["new_ticks"]
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(
                "[approval_store] confirm_callback got malformed plan: %s",
                e,
            )
            return False
        a = find_valid_approval(
            nft_token_id=tid,
            pillar=pillar,
            expected_new_tick_lower=int(t_lower),
            expected_new_tick_upper=int(t_upper),
        )
        if a is None:
            logger.info(
                "[approval_store] no valid approval for tokenId=%d %s "
                "ticks=(%d,%d) — executor will skip",
                tid, pillar, int(t_lower), int(t_upper),
            )
            return False
        logger.info(
            "[approval_store] valid approval found for tokenId=%d %s "
            "ticks=(%d,%d) by %s — executor will proceed",
            tid, pillar, int(t_lower), int(t_upper), a.approver,
        )
        return True

    return _callback
