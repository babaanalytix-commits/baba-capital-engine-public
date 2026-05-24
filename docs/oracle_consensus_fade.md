# ORACLE consensus-fade strategy (v0.5)

> Status: v0.5 shipped 2026-05-24. Asymmetric small-bet contrarian fades on heavily-concentrated Polymarket macro markets.

## Thesis

When Polymarket prices for a US macro event (FOMC / CPI / NFP / PCE / GDP) concentrate at extremes — typically the consensus market at ≥93% and the tails at ≤7% — the cheap tail is structurally underpriced in the 1-30 day window before the release.

Why the mispricing exists:

- Most participants size-weight the consensus market, ignoring the tails.
- "Boring" decisions (e.g. Fed holds) get over-priced as certainty as the event approaches.
- Last-mile reaction to Fed-speak or data revisions can shift the tail materially in the final week.
- Polymarket liquidity providers price for execution risk, not fair probability.

A $5 bet on a tail priced at $0.005 (0.5% implied probability) pays $1,000 if hit — a 200× payoff with $5 max loss. Even at a 1% true surprise rate (substantially below historical FOMC tail-realisation rates at 3+ weeks out), the expected value is positive.

## Live example (2026-05-24)

The June 18 FOMC meeting is currently priced:

- "No change in Fed rates" — YES $0.976 (97.6% implied)
- "Fed +50bps" — YES $0.0025 (0.25% implied) ← strategy fades this side
- "Fed −50bps" — YES $0.005 (0.5% implied)

A $5 bet on "Fed +50bps" gives a ~2,000× payoff if hit. The strategy emits this as a single tap-to-approve signal with the full payoff math, max loss disclosure, and AI judge verdict in the Telegram alert.

## Entry rules

- Event is in `[MIN_DAYS_TO_EVENT, MAX_DAYS_TO_EVENT]` from now (default 1–30 days).
- Matching Polymarket market exists with `liquidity_usd >= MIN_LIQUIDITY_USD` (default $100K).
- Cheapest tail (YES mid price) is in `[ABSOLUTE_FLOOR, EXTREME_LOW]` (default $0.001–$0.07).
- One signal per event per scan (no double-up on cut/hike for the same FOMC).

## Sizing

- `SIZE_USD = $5` default. Designed to be expendable — these are correlated low-frequency lottery tickets.
- Max signals per scan: 3.
- Tap-to-approve only. Never auto-fire.

## Exit rules

- TP: market resolves YES (handled by ORACLE lifecycle).
- SL: probability drift past `SL_PROB_DRIFT` (default 0.20) — if our cheap-tail moves from $0.005 → $0.20, market is now pricing the tail seriously and we lock in a 40× gain.
- Time-stop: `resolution_at_iso − 1 hour` (avoid last-minute liquidity vacuum).

## Risk disclosure

- Max loss per signal = position size (default $5). No leverage, no margin call.
- Correlated risk across events (a "Fed surprises hawkishly" event would hit multiple FOMC fades simultaneously, but that's the upside scenario).
- Polymarket settlement risk: in the rare case a market resolves ambiguously, the YES holder may receive nothing. This is why `ABSOLUTE_FLOOR` filters out markets priced below 0.1% — those typically have settlement-risk distortion.

## Free-tier sources

- US macro event calendar: hand-curated 12-month rolling schedule (`engine/data/us_macro_calendar.py`).
- Polymarket market state: gamma API (`https://gamma-api.polymarket.com/markets`) — public, free, no auth.
- AI judge: Tier 1 rule logic (free) + Tier 2 Gemini (free) before any human review.

No paid data sources. No paid LLM tier required.

## Operator runbook

```bash
# Smoke test (prints what would emit right now)
cd ~/baba/wealth-ecosystem
PYTHONPATH=. python3 engine/strategies/oracle_macro_consensus_fade_v1.py

# Scheduled fire (wired into existing oracle-scan plist + queue)
# Runs whenever com.baba.engine-oracle-scanner fires.
# Signals flow through the standard ORACLE AI judge + Telegram one-tap path.
```

## Roadmap

- v1.1: SL_PROB_DRIFT staircase — close 50% at 5× gain, 100% at 20×, locks in profit on partial moves.
- v1.2: cross-event correlation throttle — don't double-up if FOMC + CPI fades both fire in the same week.
- v1.3: AI judge prompt tuned to flag pathologically-illiquid tails (settlement risk).
