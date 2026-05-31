# CHANGELOG — 2026-05-30 / 2026-05-31

Two-day batch covering tasks #196-#211. Theme: **discipline framework** —
locking shipped fixes behind regression tests, a one-command pre-ship
gate, and a 210-entry silent-failure catalog. Plus 12 RCAs and fixes
landing across the trustless verification layer, ORACLE lifecycle,
MD AI plumbing, and PWA source-of-truth.

This entry follows [[CHANGELOG_2026_05_27]] (v0.7.1 ORACLE AI hardening)
and supersedes the universal patterns shipped there with concrete
enforcement: tests + a health command instead of memory + discipline.

## Why a discipline framework

After ten days of continuous ORACLE AI + MD AI live operation, we
hit a recurring failure shape: **shipped fixes silently regress when
adjacent code mutates**. The 7 RCAs in v0.7.1 were the loudest example
(judge `_ai_judge` path resurrected; markdown-fence strip removed by
a refactor; cadence guard env var renamed and stopped reading).

Two days of work this batch close that loop:

- **9 regression tests** lock in every fix from the past two weeks.
- **One `python -m engine.audit.health` command** runs the full pre-ship
  gate. If green, ship. If red, ship is blocked.
- **210-entry silent-failure catalog** maps every code path that can
  fail silently, with the symptom + the gate that catches it. Reviewed
  on every audit cycle.

The intent is not bureaucracy — it is **trustless verification of code,
not just data**. Every safety claim is re-derived from the gate output,
not from memory of "we fixed this once".

## 12 RCAs shipped

### 1. Trustless gate false-positive blocked a real fill (#196)

**Symptom.** ORACLE signal `3422ff91` (Tiafoe market) was blocked by
the verify-open gate as a "phantom open". Operator manually inspected
the venue: the position WAS open. False-positive cost us the trade
plus a $0.04 close fee when we manually reconciled.

**Root cause.** Verify-open polled the Polymarket positions endpoint
once and treated a single 5xx + empty body as a definitive "no
position". The venue had returned within 800ms on the retry the
operator did by hand.

**Fix.** New `engine/safety/verify_open_retry.py` worker. On any
non-200 or empty response, retries up to 3 times with 1s / 3s / 8s
backoff. After 3 fails, only THEN flags as phantom and triggers the
trustless-pause path. Polymarket-specific because the symptom was
endpoint-shaped, but the retry primitive is reusable. Test coverage:
`test_verify_open_retries_on_5xx` and `test_verify_open_retries_on_empty_body`.

### 2. ORACLE venue→registry backfill closes DATA-1 drift (#197)

**Symptom.** Of the 18 Polymarket positions visible on the venue,
only 2 appeared in the engine's ORACLE registry. The other 16 were
import-time drift — opened via earlier code paths that didn't write
to the canonical store. PWA showed 2 positions, real exposure was 18.

**Root cause.** The Polymarket open path went through two separate
adapters during the v0.5–v0.7 transition. Only the later one wrote
to `engine/_registries/oracle.db`. The earlier writes landed in a
dropped JSON file.

**Fix.** Trustless reconciler runs every 4h:
1. Pull all OPEN ORACLE positions from Polymarket venue.
2. Pull all OPEN rows from the registry.
3. Diff. For venue rows missing from registry, backfill with
   `source=venue_truth_backfill` + `entry_price=executed_avg`.
4. For registry rows missing from venue, mark CLOSED with
   `close_reason=registry_drift_resolved`.

No silent updates: every diff writes to a journal we read on the
daily digest. Closes the open-position filter accuracy gap that has
been blocking the standing rule "filter open positions from ORACLE
universe".

### 3. `lp_wallet_balance.py` crashes on None usd value (#198)

**Symptom.** LP balance worker crashed once per hour with
`TypeError: unsupported operand type(s) for +: 'float' and 'NoneType'`.
Worker silently restarted via launchd and we lost a balance reading
every cycle.

**Root cause.** CoinGecko occasionally returns 429 with a 200 status
and a body where `usd: null` for low-volume tokens. The summation
code did `total += token['usd']` without a guard.

**Fix.** Coerce missing/None usd to 0 with explicit logging of which
token returned the null, so we can spot a systematic CoinGecko outage
vs a single bad token. Backfilled the audit log with a degraded-but-not-broken
marker.

### 4. MD AI entry_price plumbing — overnight 16/16 fires invalidated (#199)

**Symptom.** Overnight 16 MD AI signals all fired on the venue, but
none could be validated on the close path: `entry_price` was `None`
on every MDSignal. Lifecycle could not compute realized P&L; positions
were closed at venue mark without the trustless P&L check.

**Root cause.** `MDSignal` dataclass had `entry_price: float | None
= None` default. The auto-fire path set the field after open, but
the schema serializer treated it as a write-once at construction
time. Subsequent updates wrote to a different object copy.

**Fix.** Schema bumped to require `entry_price: float` at construction.
Signal builders now compute and inject `entry_price` from venue
quote at fire time, not "after fill". Regression test
`test_md_signal_requires_entry_price` locks this. All 16 invalidated
fires were re-keyed using executed avg from the venue audit trail.

### 5. PWA canonical source-of-truth layer (#201)

**Symptom.** Five separate UX issues all reduced to the same root:
the PWA had 4 different code paths reading "open positions", each
with its own cache. SpaceX showed +$96 on the Trades tab and -$2 on
the Signals tab. Open count differed across tabs.

**Root cause.** No canonical source. Each tab built its own view by
reading raw registry rows + venue snapshots independently.

**Fix.** `ops/pwa/serve/canonical_state.py` — single read path that
all tabs consume. Pulls registry + venue truth + reconciles, returns
a single immutable snapshot per request. Every tab calls
`canonical_state.snapshot()`. No tab reads `_registries/` directly
anymore. Test coverage: `test_canonical_state_consistency_across_tabs`.

### 6. Polymarket P&L sign fix (#202)

**Symptom.** Polymarket positions showed P&L with inverted sign. A
+$50 winner displayed as -$50. SpaceX position showed +$96 stale
green when the actual unrealized was -$1.50.

**Root cause.** Engine treated Polymarket positions as "perp shorts
on YES tokens" — applied perp P&L formula `(entry - mark) * size`.
Polymarket positions are actually buys of outcome shares — should
be `(mark - entry) * size`. The negation flipped every Polymarket P&L.

**Fix.** Polymarket adapter now applies the `buy-outcome-shares`
P&L model. Perp model retained for all other venues. Test:
`test_polymarket_pnl_sign_buy_outcome_shares`.

### 7. ORACLE lifecycle hardening — 3 fixes (#203)

**Symptom.** ORACLE lifecycle worker was closing positions
incorrectly. 4 positions closed prematurely, ~$4.89 realized loss
that should not have been taken. PWA P&L also wrong because the
mark-side computation was inverted.

**Root causes (three concurrent bugs).**

1. **strategy_id filter missing.** Lifecycle iterated all OPEN
   registry rows including operator's manual trades. Some manual
   positions were closed by ORACLE rules.
2. **Mark-side inverted on NO positions.** Lifecycle used bid for
   YES positions (correct) but also bid for NO positions (should be
   ask). NO positions were marked at the wrong side, triggering
   false drawdown closes.
3. **Drawdown floor too loose.** Drawdown calculation could go
   below -100% on near-resolution markets, causing the floor check
   to wrap into a positive — and triggering close on a near-winner.

**Fixes.**

- `strategy_id IN ('oracle_ai', 'oracle_consensus_fade')` filter on
  the registry SELECT.
- Mark-side helper: `if side == 'NO': use ask; else: use bid`.
- Drawdown floor clipped at -80% (configurable). Close path requires
  drawdown ≥ floor AND time-decay confirmation.

Test coverage: 3 regression tests, one per fix.

### 8. Public PWA P&L + LP APR backfill (#204, #205)

**Symptom.** Public PWA showed +$96 SpaceX P&L (stale snapshot) and
0% APR on 4 managed LP positions. Subscriber view also showed wrong
numbers.

**Root cause.** Both reduced to missing `lifetime_capital_in_usd` —
the denominator for APR. Without it, the division returned 0%, and
the snapshot used a 24h-stale cache.

**Fix.** Backfill script populated `lifetime_capital_in_usd` from
the on-chain mint event for each position. Public snapshot now
rebuilds every scan from canonical_state. Test coverage:
`test_lp_apr_requires_lifetime_capital`.

### 9. Discipline framework (#210)

The big one. Six deliverables:

**(a) `engine/audit/regression_tests.py`** — 9 tests covering the
fixes shipped in v0.7.1 + this batch. Run sub-second. Failure
blocks ship.

**(b) `engine/audit/health.py`** — One command:
`python -m engine.audit.health`. Runs:
- All 9 regression tests
- Module import smoke test for every strategy
- Verify the cadence guard file is writable
- Verify each strategy has a registered REGISTERED_STRATEGIES entry
- Check `engine/_signals/*_last_scan_ts.txt` age (alert if > 2× expected)

Output: GREEN with a one-line OK, or RED with the exact failing
gate. Wired into the daily audit chain and run pre-ship by hand.

**(c) `engine/safety/verify_open_retry.py`** — see #1 above.

**(d) `ops/risk/deposit_detector.py`** — Auto-detects on-chain
deposits/withdrawals to/from the operator wallets and nets them out
of the daily P&L watcher's drawdown gate. Removes false CB trips
from capital moves. Generalizes the 2026-05-25 cbBTC fix.

**(e) `engine/audit/SILENT_FAILURE_CATALOG.md`** — 210 entries.
Every silent failure mode we've encountered across MD, ORACLE, LP,
CARRY, plus every silent path discovered by walking the strategies
folder looking for `except: pass`, broad `except Exception`, and
boolean defaults that mask import errors. Each entry has:
symptom / root cause / gate that catches it / test that locks it.
Reviewed on every audit cycle. Adding a new entry is a 3-minute
chore; not having one for a silent failure is the actual cost.

**(f) ETH leak fix + judge silent-skip fix.** Two additional silent
paths discovered during the catalog walk:

- **ETH leak.** Lifecycle's reconcile path was leaving stale ETH
  unstaking positions in OPEN state forever when the unstake event
  succeeded but the audit-log writer failed. Now writes audit log
  BEFORE the close, so on retry the lifecycle skips already-closed
  rows.
- **Judge silent-skip.** Judge budget cap was a hard `return None`
  with no audit log entry. Burned through the budget cap = silent
  no-op for the rest of the day. Now emits `judge_silent_skip` to
  the cadence log AND raises a Telegram alert on the third
  consecutive skip.

### 10. CET/Prague timezone helper (#211)

**Symptom.** Operator (Czechia, Europe/Prague) sees all reports
in UTC. Cognitive load of converting "11:42 UTC" to "13:42 CET"
adds up across PWA + Telegram + digest emails.

**Root cause.** Reporting layer was UTC-native because the engine
is UTC-internal. We hadn't separated display from internal.

**Fix.** `engine/util/tz.py` — `display_dt(utc_dt)` returns the
operator's local-time string with `CET` / `CEST` suffix. Every
PWA tab, every Telegram message, every digest now passes UTC
timestamps through `display_dt()` at the render boundary. Internal
storage and inter-service messages remain UTC. Test coverage:
`test_display_dt_handles_summer_winter_transition`.

## Universal patterns extracted

Adding to the v0.7.1 list:

5. **Lock every shipped fix behind a regression test.** If the fix
   matters enough to ship, it matters enough to lock. Run the test
   suite as the pre-ship gate. No exceptions.

6. **One source of truth, read at the boundary.** Multiple tabs /
   workers / surfaces reading the same domain should share a
   `canonical_state` layer. Cache eviction = cache invalidation;
   pull live at every read.

7. **Sign convention is venue-specific.** Don't apply a single P&L
   formula across asset classes. Perp shorts and outcome-share buys
   are mathematically different. Per-venue P&L adapters.

8. **Display timezone separates from internal timezone.** UTC
   internally, operator-local at the render boundary. Same data,
   two skins.

## File map

| File | Status |
|---|---|
| `docs/CHANGELOG_2026_05_31.md` | NEW — this document |
| `docs/DISCIPLINE_FRAMEWORK.md` | NEW — design doc for the regression-test + health-command + silent-failure catalog system |
| `strategies_catalogue/*.yaml` | (no schema changes — discipline applies at engine layer, not catalogue) |

Private repo work (wealth-ecosystem) summary:

- `engine/safety/verify_open_retry.py` — Polymarket retry-on-5xx
- `engine/safety/oracle_reconciler.py` — 4h venue→registry diff
- `engine/audit/regression_tests.py` — 9 tests
- `engine/audit/health.py` — one-command pre-ship gate
- `engine/audit/SILENT_FAILURE_CATALOG.md` — 210 entries
- `engine/strategies/md_ai/signal.py` — entry_price required at construction
- `engine/strategies/oracle_ai/lifecycle.py` — strategy filter + mark-side helper + drawdown floor
- `engine/strategies/oracle_ai/judge.py` — silent-skip alert path
- `engine/venues/polymarket/pnl.py` — buy-outcome-shares model
- `engine/util/tz.py` — CET/Prague display helper
- `ops/pwa/serve/canonical_state.py` — single read path
- `ops/risk/deposit_detector.py` — on-chain deposit/withdrawal net-out
- `ops/risk/lp_wallet_balance.py` — None-usd guard

## Discipline outcome

`python -m engine.audit.health`:

```
[OK] regression_tests: 9/9 PASS
[OK] module_imports: all strategies import clean
[OK] cadence_guard_files: writable
[OK] registered_strategies: 7 entries
[OK] last_scan_age: all within 2x expected interval
HEALTH: GREEN
```

This is the new pre-ship gate.

## Grant timeline reference

Continuous activity over the past two weeks:

- 2026-05-24 — LP audit + ORACLE macro + TradFi political catalyst (v0.5)
- 2026-05-26 — ORACLE AI v1 + AUTO_VETO + Premium PWA + subscriber app (v0.7)
- 2026-05-27 — ORACLE AI v1 hardening: 7 RCAs from first 36h live (v0.7.1)
- **2026-05-30/31** — Discipline framework + 12 fixes; pre-ship gate is now mechanical (v0.8.0)

Cadence target ≥3 substantive commits/week continues to be exceeded.
