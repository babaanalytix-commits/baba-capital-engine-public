# CHANGELOG — 2026-05-27

Continuous-active-development update. 7 RCAs + fixes shipped today after
ORACLE AI v1 hit edge cases in real production over its first 36 hours
live. All ORACLE AI infrastructure now battle-tested.

This entry follows [[CHANGELOG_2026_05_26]] which shipped the strategy
itself. Today's work is hardening + universal pattern extraction for the
next AI strategy (MD AI v1).

## 7 RCAs shipped

### 1. Gemini Cloud Prepay vs Cloud Billing — two separate credit pools

**Symptom.** Gemini API returned `Your prepayment credits are depleted`
despite operator paying 300 CZK to the Cloud Billing account. Single
curl test responded with `OK`, but scanner's batch calls kept failing
with 429 RESOURCE_EXHAUSTED.

**Root cause.** Google has two distinct credit pools for Gemini API
Tier 1: the general Cloud Billing balance (where the 300 CZK landed)
and a separate Cloud Prepay pool (which Gemini Tier 1 actually charges
against). Topping up Cloud Billing does NOT fund Cloud Prepay. UI surfaces
this only via the "Set up prepay" link in AI Studio API Keys page.

**Fix.** Funded Cloud Prepay directly (CZK 300 + auto-reload at CZK 10).
Documented as a reference memory so future operators don't re-encounter
the same trap. Conservative reload threshold per [[feedback_prove_then_automate]]:
fund only what's needed to test the thesis.

### 2. Haiku triage JSON markdown-fence parse bug

**Symptom.** When Gemini was depleted and Haiku fallback engaged, every
triage batch returned 0 PASS. Strategy looked alive (real tokens
consumed, costs logged) but produced no signals. Bug went undetected
overnight because all-SKIP is a plausible content outcome.

**Root cause.** Anthropic models wrap JSON output in markdown code
fences (\`\`\`json ... \`\`\`) even when explicitly told not to. The
parser called json.loads() on the raw text-with-fences, got a parse
error, defaulted every market to SKIP.

**Fix.** Strip fences defensively via first-`[` / last-`]` slicing in
`_call_haiku`. Strengthened system prompt to require "first char must
be '['". Universal pattern documented for any future AI strategy
calling Claude for JSON output.

### 3. Sonnet judge: same markdown-fence parse bug

**Symptom.** After Gemini billing recovered, ORACLE AI emitted 3 PASS
verdicts at triage. All 3 returned PARSE_ERROR at judge layer ($0.04
wasted). 0 signals fired despite real candidates.

**Root cause.** Claude Sonnet does the same markdown-fence wrapping as
Haiku, but for single objects instead of arrays. The previous defensive
strip only handled `if body_text.startswith("\`\`\`")` — Claude often
prefixes with prose ("Here's my verdict:\n\`\`\`json...") so the naive
check missed the case.

**Fix.** Robust strip via first-`{` / last-`}` for object-shaped
responses. Same pattern as triage but for objects. Prompt strengthened
to "first character MUST be '{', last MUST be '}'". Diagnostic log
emits the body tail (last 200 chars) on parse failure to catch new
wrapping variants.

### 4. Cadence guard against runaway scan rate

**Symptom.** Scanner firing 3 scans within 100 seconds, then 45-min gap,
then another burst. Each scan in Haiku-fallback mode = ~$0.08, so each
burst burned $0.24. At 30+ bursts/day this would exhaust the $5
Anthropic budget cap in <14 hours.

**Root cause.** Multiple `launchctl kickstart` commands queued during
yesterday's debugging session were still being honored. The plist
StartInterval value on disk was 7200s but launchctl held onto pending
fires from earlier.

**Fix.** Persistent file-based cadence guard in `strategy.py::scan()`.
Refuses to scan if last scan started within `MIN_SCAN_INTERVAL_SEC`
(default 6600s = 110min). Survives across process invocations via
`engine/_signals/oracle_ai_last_scan_ts.txt`. Env-tunable for testing
with `ORACLE_AI_MIN_SCAN_INTERVAL_SEC=60` one-shot override.

### 5. Size auto-bump extended to absolute floor

**Symptom.** ORACLE AI sized a signal at $3.90 (78% confidence × $5
MAX_SIZE). Validator REJECTed with "size below absolute floor $5.0".
Auto-bump shipped previously for venue minimums didn't help because
Polymarket venue_min is $1 — the absolute SIZE_HARD_MIN_USD floor was
the constraint.

**Root cause.** Validator has TWO size floors: per-venue
`min_order_value_usd` and engine-wide `SIZE_HARD_MIN_USD`. The 2026-05-26
auto-bump only consulted the venue floor.

**Fix.** Extended auto-bump to use `max(venue_min, SIZE_HARD_MIN_USD)`
as the effective floor. Same 70% bump-ratio tolerance. Now $3.90 →
$5.00 fires cleanly.

### 6. send_with_keyboard regression — per-signal Telegram messages
never fired

**Symptom.** Operator received batched digest Telegram messages but
no per-signal tappable messages with the `🤖 ORACLE AI Signal` header
+ approve button. Discovered when operator asked "FYI it doesn't say
it's from AI ORACLE".

**Root cause.** Per-signal sender imported a non-existent function
(`send_with_keyboard`) — the real telegram client API is
`send(category, key, text, keyboard=[...])`. Bug had been silent since
the AI badge was shipped because the failing import was caught by a
broad `except` and only logged as a WARNING.

**Fix.** Replaced bad import with the real `send()` API. Same fix
applied to the AUTO_VETO sender that would have failed on first
attempt to use that mode. Lesson: prefer specific exception types
in try/except blocks so import errors aren't silently swallowed.

### 7. Duplicate-open guard at engine layer

**Symptom.** Operator opened the same ORACLE AI signal twice — once
via Telegram tap, once via PWA tap. Two identical positions on
Polymarket from one signal. Each approval path generated fresh
idem_keys, so the engine's existing internal dedup didn't catch it.

**Root cause.** Engine idempotency was keyed at the EXECUTION level
(pillar+venue+asset+side+intent) but multiple approval paths to the
same execution don't necessarily collide. Approval-time dedup didn't
exist.

**Fix.** Engine-wide duplicate-open guard at `engine/cli.py` open
command. Before executing, queries every pillar registry
(`engine/_registries/*.db`) for OPEN positions matching
`(venue, asset, side)` from the same strategy family within
DUPLICATE_WINDOW_SEC=600s. If found, REJECTs with exit code 5 + clear
explanation. Universal — applies to all approval paths (Telegram, PWA,
manual CLI) because they all converge to engine cli open. Override:
`--skip-dedup`.

## Universal patterns extracted (for next AI strategy: MD AI v1)

The 7 fixes above distill into 4 universal patterns the next AI strategy
must bake in from day one:

1. **Provider fallback latch** — when primary model returns
   RESOURCE_EXHAUSTED / persistent 429, auto-switch to fallback provider
   for the remainder of the scan. Reset latch on next scan. Same pattern
   covers Gemini→Anthropic and Anthropic→Gemini equally well.

2. **Markdown-fence-strip on every Anthropic JSON call** — Anthropic
   models wrap JSON in markdown fences regardless of prompting. Use
   first-`[` / last-`]` for arrays, first-`{` / last-`}` for objects.
   Strengthen prompt with "first char must be X, last must be Y" as
   defense-in-depth.

3. **File-persisted cadence guard at scan() entry** — every long-running
   scan worker needs a self-defense against repeated kickstarts.
   `engine/_signals/<strategy>_last_scan_ts.txt` + min interval check is
   ~10 lines and prevents budget runaway.

4. **Duplicate-execution guard at the CHOKE POINT** — for any strategy
   with multiple approval paths (Telegram, PWA, CLI), guard at the
   execution layer (engine.cli.open), not at each approval entrypoint.
   Universal coverage with single source of truth.

## File map

| File | Status |
|---|---|
| `docs/CHANGELOG_2026_05_27.md` | NEW — this document |
| `strategies_catalogue/oracle.yaml` | (no changes — universal patterns belong in CHANGELOG, not per-strategy schema) |

Private repo work (wealth-ecosystem) summary:

- `engine/strategies/oracle_ai/triage.py` — markdown-fence strip, prompt
  strengthen, Haiku fallback signature now returns
  `(resp, exhaustion_reason)`
- `engine/strategies/oracle_ai/judge.py` — markdown-fence strip for
  object-shaped JSON, prompt strengthen
- `engine/strategies/oracle_ai/strategy.py` — cadence guard with
  file-persisted timer
- `engine/strategies/oracle_ai/cost_ledger.py` — silent-failure
  detection (N consecutive 0-token / 0-cost SKIPs auto-pauses)
- `engine/cli.py` — extended size auto-bump for absolute floor,
  duplicate-open guard for all strategies

## Grant timeline reference

Continuous activity over the past month:

- 2026-05-12 — initial public release (v0.1)
- 2026-05-16 — venue-side SL detection doc
- 2026-05-24 — LP audit + ORACLE macro scanner + TradFi political catalyst (v0.5)
- 2026-05-24 — ORACLE consensus-fade strategy (v0.5.2)
- 2026-05-26 — ORACLE AI v1 + AUTO_VETO + Premium PWA + subscriber app (v0.7)
- **2026-05-27** — ORACLE AI v1 hardening: 7 RCAs from first 36h live (v0.7.1)

Cadence target ≥3 substantive commits/week is being exceeded.
