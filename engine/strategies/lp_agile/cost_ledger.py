"""engine/strategies/lp_agile/cost_ledger.py — per-position cost tracker.

Per Yomi 2026-05-23 directive: bottom-line returns must include EVERY cost.
Records every $ that flows in/out of an LP position so net APR is honest.

Storage: NDJSON at engine/_state/lp_cost_ledger.ndjson (one line per event).
Append-only. Trustless: each event tagged with on-chain tx_hash so any number
can be re-verified from the chain at any time.

Events tracked (in chronological order for a typical cycle):
  1. open_swap        — pre-mint USDC→volatile swap (fee + slippage cost)
  2. open_approve     — approve(token0) and approve(token1) gas
  3. open_mint        — mint NFT gas + entry NFT value
  4. fee_collected    — accrued LP fees (collect() on NPM)
  5. close_decrease   — decreaseLiquidity gas
  6. close_collect    — collect gas
  7. close_swap       — post-close volatile→USDC swap (fee + slippage)
  8. position_closed  — summary record with realised P&L
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.cost_ledger")

LEDGER_PATH = (
    Path(__file__).resolve().parents[2] / "_state" / "lp_cost_ledger.ndjson"
)


@dataclass(frozen=True)
class LPCostEvent:
    """One cost event in a position's lifecycle."""
    position_id: str                      # links open → fees → close events
    event_type: str                       # see module docstring
    timestamp_iso: str
    tx_hash: Optional[str]
    chain: str
    # $ economics
    gas_cost_usd: Decimal                 # gas burnt on this tx
    cost_usd: Decimal                     # additional $ cost (swap slippage, etc.)
    benefit_usd: Decimal                  # any $ received (fees collected)
    # Token movements
    token0_delta: Decimal = Decimal(0)    # +received / -spent
    token1_delta: Decimal = Decimal(0)
    notes: str = ""

    def to_json(self) -> str:
        d = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}
        return json.dumps(d)


def log_event(event: LPCostEvent) -> None:
    """Append one event to the ledger (creates file + parent dir if missing)."""
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("a") as f:
        f.write(event.to_json() + "\n")
    logger.info("ledger event: %s pos=%s gas=$%s cost=$%s benefit=$%s",
                event.event_type, event.position_id,
                event.gas_cost_usd, event.cost_usd, event.benefit_usd)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Convenience builders for the executor
# ---------------------------------------------------------------------------


def log_swap(
    position_id: str, *, tx_hash: str, chain: str,
    amount_in_human: Decimal, amount_out_human: Decimal,
    expected_out_human: Decimal, gas_cost_usd: Decimal,
    direction: str,  # "open" or "close"
) -> None:
    """Log a swap event with realised slippage as cost."""
    slip_units = expected_out_human - amount_out_human
    slip_cost = slip_units if slip_units > 0 else Decimal(0)
    log_event(LPCostEvent(
        position_id=position_id,
        event_type=f"{direction}_swap",
        timestamp_iso=_now(),
        tx_hash=tx_hash,
        chain=chain,
        gas_cost_usd=gas_cost_usd,
        cost_usd=slip_cost,
        benefit_usd=Decimal(0),
        notes=f"in={amount_in_human} out={amount_out_human} expected={expected_out_human}",
    ))


def log_approve(
    position_id: str, *, tx_hash: str, chain: str,
    token_symbol: str, gas_cost_usd: Decimal,
) -> None:
    log_event(LPCostEvent(
        position_id=position_id,
        event_type="open_approve",
        timestamp_iso=_now(),
        tx_hash=tx_hash,
        chain=chain,
        gas_cost_usd=gas_cost_usd,
        cost_usd=Decimal(0),
        benefit_usd=Decimal(0),
        notes=f"approve({token_symbol})",
    ))


def log_mint(
    position_id: str, *, tx_hash: str, chain: str,
    nft_token_id: int, deposited_usd: Decimal,
    gas_cost_usd: Decimal,
) -> None:
    log_event(LPCostEvent(
        position_id=position_id,
        event_type="open_mint",
        timestamp_iso=_now(),
        tx_hash=tx_hash,
        chain=chain,
        gas_cost_usd=gas_cost_usd,
        cost_usd=Decimal(0),
        benefit_usd=Decimal(0),
        notes=f"nft={nft_token_id} deposited=${deposited_usd}",
    ))


def log_fee_collected(
    position_id: str, *, tx_hash: str, chain: str,
    token0_fees_usd: Decimal, token1_fees_usd: Decimal,
    gas_cost_usd: Decimal,
) -> None:
    total_fees = token0_fees_usd + token1_fees_usd
    log_event(LPCostEvent(
        position_id=position_id,
        event_type="fee_collected",
        timestamp_iso=_now(),
        tx_hash=tx_hash,
        chain=chain,
        gas_cost_usd=gas_cost_usd,
        cost_usd=Decimal(0),
        benefit_usd=total_fees,
        notes=f"t0=${token0_fees_usd} t1=${token1_fees_usd}",
    ))


def log_close_decrease(
    position_id: str, *, tx_hash: str, chain: str,
    gas_cost_usd: Decimal, withdrawn_usd: Decimal,
) -> None:
    log_event(LPCostEvent(
        position_id=position_id,
        event_type="close_decrease",
        timestamp_iso=_now(),
        tx_hash=tx_hash,
        chain=chain,
        gas_cost_usd=gas_cost_usd,
        cost_usd=Decimal(0),
        benefit_usd=Decimal(0),
        notes=f"withdrawn=${withdrawn_usd}",
    ))


# ---------------------------------------------------------------------------
# Impermanent loss (LP-2, 2026-05-30)
# ---------------------------------------------------------------------------
# Before this, net P&L was fee + gas + swap-slippage only — it had NO IL term,
# so reported LP "net" overstated true performance on any position that saw
# price divergence. IL is the difference between holding the entry basket and
# what the LP position actually gave back. Two ways to get it:
#   1. realized_il_from_baskets — EXACT, model-free: uses the ACTUAL token
#      amounts deposited vs withdrawn (preferred; uses on-chain truth).
#   2. realized_il_from_model — when only prices + deposit are known, uses the
#      concentrated-liquidity (V3) value math (same as the LP backtest).
# IL is stored as an `il_realized` event whose cost_usd is the signed IL (a
# positive cost = the LP underperformed holding). summarize_position breaks it
# out so fees / IL / gas are each visible and net is finally honest.

IL_EVENT_TYPE = "il_realized"


def realized_il_from_baskets(
    *, entry_token0: Decimal, entry_token1: Decimal,
    exit_token0: Decimal, exit_token1: Decimal, exit_price0: Decimal,
) -> dict:
    """EXACT realized IL from actual token balances (token1 = quote/USDC ≈ $1).

    hodl_value = the ENTRY basket valued at EXIT prices (what you'd have by
    holding); lp_value = what the position actually returned at exit. IL is the
    shortfall: hodl_value - lp_value (>= 0 is a drag; < 0 means LP beat holding).
    """
    hodl_value = entry_token0 * exit_price0 + entry_token1
    lp_value = exit_token0 * exit_price0 + exit_token1
    return {"il_usd": hodl_value - lp_value,
            "hodl_value_usd": hodl_value, "lp_value_usd": lp_value}


def realized_il_from_model(
    *, entry_price: Decimal, exit_price: Decimal, deposited_usd: Decimal,
    range_low: Decimal, range_high: Decimal,
) -> dict:
    """Concentrated-liquidity (V3) IL when only prices + deposit are known.

    Reuses the backtest's V3 position-value math so live + backtest agree.
    """
    from engine.backtest.lp_replay import _amounts, _value, _liquidity_for_deposit
    P0, P1 = float(entry_price), float(exit_price)
    Pa, Pb = float(range_low), float(range_high)
    L = _liquidity_for_deposit(float(deposited_usd), P0, Pa, Pb)
    e0, e1 = _amounts(L, P0, Pa, Pb)               # entry basket
    hodl_value = e0 * P1 + e1                        # held to exit
    lp_value = _value(L, P1, Pa, Pb)                # LP position at exit
    return {"il_usd": Decimal(str(hodl_value - lp_value)),
            "hodl_value_usd": Decimal(str(hodl_value)),
            "lp_value_usd": Decimal(str(lp_value))}


def log_il_realized(
    position_id: str, *, il_usd: Decimal,
    hodl_value_usd: Optional[Decimal] = None,
    lp_value_usd: Optional[Decimal] = None,
    tx_hash: Optional[str] = None, chain: str = "", notes: str = "",
) -> None:
    """Log realised IL as a signed cost. Positive il_usd = LP underperformed
    holding (a real cost); negative = LP beat holding (recorded as a benefit)."""
    il = Decimal(str(il_usd))
    log_event(LPCostEvent(
        position_id=position_id,
        event_type=IL_EVENT_TYPE,
        timestamp_iso=_now(),
        tx_hash=tx_hash,
        chain=chain,
        gas_cost_usd=Decimal(0),
        cost_usd=il if il > 0 else Decimal(0),
        benefit_usd=(-il) if il < 0 else Decimal(0),
        notes=notes or f"il=${il} hodl=${hodl_value_usd} lp=${lp_value_usd}",
    ))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionSummary:
    position_id: str
    n_events: int
    total_gas_usd: Decimal
    total_other_costs_usd: Decimal       # swap slippage etc. — EXCLUDES IL
    total_fees_usd: Decimal              # fees collected (the gross upside)
    total_il_usd: Decimal                # signed: + = IL drag, - = LP beat hodl
    total_benefits_usd: Decimal          # fees + any IL-beat benefit
    net_pnl_usd: Decimal                 # IL-AWARE headline: fees - gas - other - il
    net_pnl_excl_il_usd: Decimal         # old fee-only view, for comparison
    open_timestamp: Optional[str]
    last_event_timestamp: Optional[str]


def summarize_position(position_id: str) -> Optional[PositionSummary]:
    """Aggregate all events for a position into an IL-AWARE P&L summary."""
    if not LEDGER_PATH.exists():
        return None
    events: list[dict] = []
    for line in LEDGER_PATH.read_text().splitlines():
        try:
            d = json.loads(line)
            if d.get("position_id") == position_id:
                events.append(d)
        except json.JSONDecodeError:
            continue
    if not events:
        return None

    def _is_il(e: dict) -> bool:
        return e.get("event_type") == IL_EVENT_TYPE

    total_gas = sum((Decimal(e["gas_cost_usd"]) for e in events), Decimal(0))
    # Split costs: IL events vs everything else (swap slippage etc.)
    total_other = sum((Decimal(e["cost_usd"]) for e in events if not _is_il(e)), Decimal(0))
    # Signed IL: cost portion (drag) minus benefit portion (LP beat hodl)
    total_il = sum((Decimal(e["cost_usd"]) - Decimal(e["benefit_usd"])
                    for e in events if _is_il(e)), Decimal(0))
    total_fees = sum((Decimal(e["benefit_usd"]) for e in events if not _is_il(e)), Decimal(0))
    total_benefit = sum((Decimal(e["benefit_usd"]) for e in events), Decimal(0))

    net_excl_il = total_fees - total_gas - total_other
    net = net_excl_il - total_il
    return PositionSummary(
        position_id=position_id,
        n_events=len(events),
        total_gas_usd=total_gas,
        total_other_costs_usd=total_other,
        total_fees_usd=total_fees,
        total_il_usd=total_il,
        total_benefits_usd=total_benefit,
        net_pnl_usd=net,
        net_pnl_excl_il_usd=net_excl_il,
        open_timestamp=events[0]["timestamp_iso"],
        last_event_timestamp=events[-1]["timestamp_iso"],
    )


def summarize_all_positions() -> list[PositionSummary]:
    """One PositionSummary per distinct position_id in the ledger."""
    if not LEDGER_PATH.exists():
        return []
    seen: set[str] = set()
    for line in LEDGER_PATH.read_text().splitlines():
        try:
            d = json.loads(line)
            pid = d.get("position_id")
            if pid:
                seen.add(pid)
        except json.JSONDecodeError:
            continue
    return [summarize_position(pid) for pid in sorted(seen)]
