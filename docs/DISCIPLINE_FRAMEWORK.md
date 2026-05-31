# Discipline Framework

The discipline framework is the engine's answer to a recurring failure
shape: **shipped fixes silently regress when adjacent code mutates**.

Three pieces:

1. **Regression test suite** — every shipped fix is locked behind a
   sub-second test that re-asserts the invariant the fix established.
2. **One-command health gate** — `python -m engine.audit.health`
   runs the full pre-ship check in under 5 seconds. Green or Red,
   no judgement.
3. **Silent-failure catalog** — 210 documented failure modes, each
   with symptom, root cause, gate that catches it, and test that
   locks it. Reviewed on every audit cycle.

## Why mechanical, not memory

Through v0.5–v0.7 we relied on "we fixed this once" memory + careful
review to prevent regression. It did not work. Two examples:

- The `_ai_judge` legacy path was killed in v0.6 and resurrected in
  v0.7 by a search-and-replace that thought the killed path was
  still canonical.
- The markdown-fence strip was added in v0.7.1, removed in a refactor
  the next day because the reviewer didn't know why the slicing was
  there.

Both regressions would have been blocked by a 5-line regression test.
Neither test existed because we treated the fix as "done when shipped".

The discipline framework treats a fix as **done when locked**.

## The regression test suite

Location: `engine/audit/regression_tests.py`

9 tests as of v0.8.0, covering:

1. `test_judge_strips_markdown_fence_object` — judge.py must strip
   ```json prefixes on Anthropic responses
2. `test_triage_strips_markdown_fence_array` — same for triage arrays
3. `test_cadence_guard_blocks_repeat_scan_within_interval` — cadence
   guard must refuse a scan within MIN_SCAN_INTERVAL_SEC
4. `test_size_auto_bump_respects_absolute_floor` — auto-bump must
   use max(venue_min, SIZE_HARD_MIN_USD)
5. `test_duplicate_open_guard_blocks_within_window` — engine cli
   open must refuse a duplicate within DUPLICATE_WINDOW_SEC
6. `test_verify_open_retries_on_5xx` — Polymarket verify retries up
   to 3 times before flagging phantom
7. `test_md_signal_requires_entry_price` — MDSignal construction
   must fail without entry_price
8. `test_polymarket_pnl_sign_buy_outcome_shares` — Polymarket P&L
   uses (mark - entry) * size, not perp formula
9. `test_lp_apr_requires_lifetime_capital` — LP APR computation
   must refuse to return a number if lifetime_capital_in_usd is None

Each test is sub-second. The suite runs in under 5 seconds.

Adding a new test is a 5-minute chore when shipping a fix. Not having
one when a fix regresses is the actual cost.

## The health command

Location: `engine/audit/health.py`

Single entry point: `python -m engine.audit.health`

Runs five gates:

1. **regression_tests** — runs the suite above
2. **module_imports** — imports every strategy module, catches
   ImportError that would silently disable a strategy
3. **cadence_guard_files** — verifies `engine/_signals/*_last_scan_ts.txt`
   are writable; warns if any are missing
4. **registered_strategies** — verifies `REGISTERED_STRATEGIES` dict
   has every strategy with a strategy.py file
5. **last_scan_age** — verifies the most recent scan for each
   strategy is within 2× the expected interval

Exit codes:
- 0 = GREEN, ship clear
- 1 = RED, ship blocked, output names the failing gate

Wired into:
- The daily audit chain (runs at 02:00 UTC)
- The pre-ship manual gate (operator runs by hand before any push)
- The post-restart smoke test

## The silent-failure catalog

Location: `engine/audit/SILENT_FAILURE_CATALOG.md`

210 entries as of v0.8.0. Each entry:

```
### <symptom>

**Where.** <module:function>
**Root cause.** <why the failure is silent>
**Gate that catches it.** <regression test / health check / audit hook>
**Test.** <test name in regression_tests.py>
```

Categories:
- Provider fallback latches (Gemini ↔ Anthropic)
- JSON parse robustness (markdown-fence variants)
- Cache invalidation (canonical_state boundaries)
- Sign / unit conventions (P&L, APR, leverage)
- Schema drift (dataclass defaults, missing-field None coercion)
- launchd persistence (RunAtLoad, plist staleness)
- Telegram delivery (silent-when-healthy + per-category routing)
- Registry write paths (writers that silently drop columns)

Reviewed on every audit cycle. Adding an entry takes 3 minutes.
Walking the catalog before a big refactor takes 30 minutes and
flags every adjacent fix the refactor is about to break.

## Trustless verification of code

The standing rule "verify every safety claim from venue truth, not
cached flag" applies to code too. The discipline framework is the
code-side enforcement: every shipped fix is **re-derived from the
gate output on every commit**, not from memory of "we fixed this once".

## Operational cadence

- **On every fix.** Add a regression test. Add a silent-failure
  catalog entry if the failure mode is new.
- **On every commit.** `python -m engine.audit.health` must be green
  locally.
- **On every daily audit.** Health gate runs at 02:00 UTC. Failure
  pages the operator.
- **On every quarterly review.** Walk the catalog. Prune entries
  whose gate has been proven unbreakable. Add entries for new
  failure modes.

## Pre-ship checklist

```
$ python -m engine.audit.health
HEALTH: GREEN
$ git add <files>
$ git commit -m '...'
$ git push
```

If health is RED, the failing gate is in the output. Fix it. Re-run.
Only push when green.

This is the new mechanical ship gate. It replaces "I tested it
manually and it works on my machine."
