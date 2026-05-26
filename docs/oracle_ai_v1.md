# ORACLE AI v1 — Two-Layer AI Judge on Polymarket

**Shipped:** 2026-05-26  
**Status:** Live (MANUAL execution mode; AUTO_VETO scaffold default off)  
**Cost target:** ≤ $0.50/day total AI spend; $5 hard total cap

---

## Why this exists

Polymarket has thousands of active binary markets at any moment. Our
prior ORACLE strategies (`pce_v2`, `commitment_anchor_fade_v1`,
`oracle_macro_consensus_fade_v1`) each scanned a narrow slice — 10s of
markets at most. We were missing the long tail.

Two questions:

1. Can an AI model — not a hand-coded rule — find mispricings we'd never
   identify by hand?
2. Can we do it cheaply enough that the cost-to-PnL ratio stays
   defensible at small subscriber scale?

ORACLE AI v1 is the experiment. Two layers of model + hard structural
guards.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  POLYMARKET GAMMA API (public, free)                                 │
│  → fetch_candidate_markets()                                         │
│     · paginates up to 1000 markets                                   │
│     · filters: volume_24h >= $500, liquidity >= $1k,                 │
│       resolution in [2h, 7d], extreme-price hard skip                │
└──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼  ≤ 100 candidate markets
┌──────────────────────────────────────────────────────────────────────┐
│  LAYER 1 — VOLUME TRIAGE (Gemini 2.5 Flash, thinking OFF)            │
│  → triage_batch() in batches of 30                                   │
│     · ~7.8K input tokens + ~70 output tokens per batch               │
│     · cost ≈ $0.0025/batch · ~$0.018/scan                            │
│     · returns PASS / SKIP per market with                            │
│       (side, pattern, fair_value, confidence, rationale)             │
└──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼  Top 3 by confidence
┌──────────────────────────────────────────────────────────────────────┐
│  LAYER 2 — PRECISION JUDGE (Claude Sonnet 4.6)                       │
│  → judge_top_n() one market per call                                 │
│     · ~800 input + ~200 output tokens per call                       │
│     · cost ≈ $0.005/judgment · ~$0.015/scan (3 calls)                │
│     · POST-EVENT GUARD: forces explicit "WHEN does this resolve"     │
│       reasoning before emitting PASS                                 │
│     · returns PASS / WATCH / BLOCKED with sizing                     │
└──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼  PASS verdicts only
┌──────────────────────────────────────────────────────────────────────┐
│  SAFETY GATE — exec_mode.py                                          │
│  → 5 filters before signal exits the strategy                        │
└──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼  Cleared signals
┌──────────────────────────────────────────────────────────────────────┐
│  L4 CONTENT BUS DELIVERY                                             │
│  → bus.publish() with tier-gated audience                            │
│  → Telegram (bmi-premium channel) for Premium / Premium+             │
│  → Web dashboard at babacapital.app/app for Premium / Premium+       │
│  → Free tier gets teaser with subscribe CTA                          │
└──────────────────────────────────────────────────────────────────────┘
```

Per-scan cost target: triage ($0.018) + judge ($0.015) = **~$0.033/scan**.
At 2h cadence: ~12 scans/day = **~$0.40/day**, comfortably under the
$0.50/day cap.

---

## Why two layers, not one

The naive approach is one expensive model judging every market. That
breaks the cost budget at the scale we need (100+ markets per scan).

The two-layer approach exploits the cost-to-precision asymmetry:

- **Gemini Flash** at $0.30/MTok in is 10× cheaper than Claude Sonnet
  ($3.00/MTok in). It can scan everything cheaply but its precision on
  hard cases is lower.
- **Claude Sonnet** judges with first-principles reasoning. We only pay
  for it on the 3 best Gemini-flagged candidates per scan.

Net: a ~$0.033/scan budget with broad coverage. Without two-layer, we'd
either spend 10× more or scan 10× less.

---

## The POST-EVENT GUARD (RCA-driven prompt design)

The first ORACLE AI live signal — a SHORT on "S&P 500 Opens Up or Down
on May 26?" — lost 50% in minutes. The judge had reasoned the YES side
at $0.9995 was a "thin-book artifact" implying near-certainty of a
gap-up open with no justification.

**The error:** US markets had opened 4 hours earlier. SPX had opened up.
The 99.95¢ price was correct; the counterparty at 0.05¢ would have been
giving us free money. Claude had misread "opens" as future-tense.

**Hidden Polymarket behavior we hadn't accounted for:** markets stay
OPEN for hours/days after the resolving event while waiting for oracle
settlement. During that window, price collapses to 0.99/0.01 because
the answer is publicly known.

**Defenses shipped (one prompt, one code-level):**

1. **Markets.py hard skip.** Drop any market with `yes_price >= 0.97 or
   <= 0.03`. Cuts <5% of universe — real edges aren't found in extreme
   tails. Code-level so prompt drift can't bring this class of failure
   back.

2. **Judge prompt POST-EVENT GUARD.** Top-of-prompt section forces
   Claude to answer in `rationale`:
   - WHEN is the resolution-determining event?
   - Has it already occurred relative to now?
   - If yes → BLOCKED, regardless of price.
   
   The prompt lists question patterns that scream post-event drift:
   `"<X> opens up or down on <today>"`, `"<team> wins <game played
   today>"`, `"<event> by <past or today's date>"`, `"<president>
   elected in <past year>"`.
   
   Mandatory BLOCKED on extreme price + <24h to resolution unless a
   specific named future event is identified.

---

## Five safety layers

All in `cost_ledger.should_auto_pause()`. Ordered cheapest-first so
catastrophic conditions trip before slow ones.

1. **Total budget cap** — refuse if total AI spend ≥ $5
2. **Daily budget cap** — refuse if today's AI spend ≥ $0.50
3. **Rapid-loss 24h** — auto-pause if cumulative realized PnL in last
   24h ≤ -$10
4. **Single-trade blowup** — auto-pause if any signal lost > 50% of size
5. **Burn-without-resolution** — auto-pause if > $2 spent over 7 days
   with zero resolved signals (catches silent-failure modes where AI
   keeps emitting but markets aren't settling)
6. **Slow-gate signal-to-PnL ratio** — after 200 calls, require
   total realized PnL / total cost ≥ 1.0

Any trip writes `ai_crazy_trigger` to the audit log and stops further
scans until manual reset.

---

## SHADOW gate (not a label, a gate)

A separate near-miss class: setting `ORACLE_AI_SHADOW=true` had only
written `metadata.shadow_mode=true` on emitted signals. The downstream
Telegram alerter ignored that flag and fired live.

**Fix:** `if SHADOW_MODE: return []` from the strategy. No signals exit
the strategy boundary at all when shadow is on. Suppressed signals are
written to a parallel `oracle_ai_shadow_signals.jsonl` for outcome
backtest.

**Universal principle:** any boolean kill switch must gate at the point
of emission, never at the point of consumption. A single forgetful
downstream consumer turns "metadata label" into "live leak."

---

## Daemon env hot-reload

A related bug: the scanner runs as a long-running daemon. `SHADOW_MODE
= os.environ.get("ORACLE_AI_SHADOW", "true").lower() == "true"` was a
module-level constant set at import. Flipping the env var in
`engine/.env` did nothing until the daemon restarted.

**Fix:** `SHADOW_MODE` became `_is_shadow_mode()` — re-read on every
scan. Env flips take effect on next tick. Same pattern now applied to
exec mode + safety thresholds.

---

## Three execution modes (scaffold, MANUAL default)

Per-strategy env-driven (`ORACLE_AI_EXEC_MODE`):

| Mode | UX | Use when |
|---|---|---|
| `SHADOW` | No emit. Signals written to side-channel only. | New AI strategy, before any live data |
| `MANUAL` | Telegram with tap-to-approve. **Current default.** | Validation phase, human as filter |
| `AUTO_VETO` | Telegram with 60s countdown + Skip + Pause-All buttons. After veto window, fires via existing executor. | After ≥10 resolved signals + signal-to-PnL ratio > 1.0 |
| `AUTO_INSTANT` | Execute first, Telegram is notification only. | Time-critical class of trades, after long proof |

Mode lives in `engine/_signals/oracle_ai_exec_mode.json` (state file
overrides env) so it can be flipped from Telegram (`/oracle_pause`
command), PWA, or env without a deploy.

Mode transition gates (from MANUAL to AUTO_VETO):
- ≥20 ORACLE AI signals manually reviewed
- ≥10 resolved with realized_pnl_usd backfilled
- signal-to-PnL ratio > 1.0 over those resolved
- Zero extreme-price false-passes in the shadow log
- Operator comfort confirmation

---

## Distribution (tier-gated content bus)

Every PASS signal publishes to the L4 content bus with `event_type:
signal_open, subtype: oracle_ai`. Field visibility per audience tier in
`engine/content/tiers.yaml`:

- **Premium ($15/mo)** sees: asset · direction · entry · SL · TP · size
  · leverage · 1-line rationale · AI pipeline label · confidence ·
  fair_value · market URL · liquidity
- **Premium+ ($30/mo)** adds: full Claude rationale · 3 risk factors ·
  cost-per-signal · RR ratio
- **Free tier** sees: asset + side + "ORACLE AI flagged" teaser +
  upgrade CTA

Same data lands in `bmi-premium` Telegram channel (push) and the web
dashboard at `babacapital.app/app` (pull, license-key-gated). Telegram
and web are equivalent — subscriber chooses delivery preference.

---

## What we'll measure to graduate

The strategy stays MANUAL until all 5 gates pass. The signal-to-PnL
ratio is the load-bearing metric — if AI signals don't pay for the AI
calls, the strategy is wrong and shouldn't auto-execute regardless of
hit rate.

We'll publish weekly aggregate stats (hit rate · cost · realized PnL)
in the public `/insights` feed once we have ≥10 resolved signals.

---

## File map

| File | Purpose |
|---|---|
| `engine/strategies/oracle_ai/markets.py` | Polymarket gamma fetcher + filters |
| `engine/strategies/oracle_ai/triage.py` | Gemini Flash volume layer |
| `engine/strategies/oracle_ai/judge.py` | Claude Sonnet precision layer |
| `engine/strategies/oracle_ai/strategy.py` | Top-level scan() entrypoint |
| `engine/strategies/oracle_ai/cost_ledger.py` | Per-call cost log + auto-pause logic |
| `engine/strategies/oracle_ai/exec_mode.py` | MANUAL/AUTO_VETO/AUTO_INSTANT dispatcher + safety filters |
| `engine/strategies/oracle_ai/auto_executor.py` | Drains pending auto-exec queue |
| `engine/strategies/oracle_ai/backfill.py` | Resolves PnL when markets settle |
| `engine/launchd/com.baba.engine-oracle-scanner.plist` | 2h scan cadence |
| `engine/launchd/com.baba.oracle-ai-auto-executor.plist` | 10s veto-queue drain |
