# Process: DRY-RUN-first-tick before flipping any new auto-execute worker LIVE

**Status:** Standing process. Adopted 2026-05-12 from task #130 lesson.

## The lesson

When `auto_close_worker` first shipped LIVE, it had a bug where DRY-RUN
mode was *poisoning* live cooldown timestamps (#134), and a separate bug
where the spread-compression rule could fire on stale spreads (#149).
Both bugs would have surfaced ON THE FIRST TICK if we'd run a DRY-RUN
cycle BEFORE flipping `ARB_AUTO_CLOSE_LIVE=true`.

We didn't. Instead the worker ran live from cycle 1, the bugs hit real
positions, and we burned a session debugging mid-flight.

The fix is process, not code: **every new auto-execute worker MUST run
in DRY-RUN for at least one full successful cycle on production data
before LIVE flag is flipped.**

## The procedure

For any worker that takes financial action (auto-open, auto-close,
liquidation, sweep, rebalance) — both first deploy AND any change to its
decision logic — do this in order:

### 1. Confirm DRY-RUN flag exists and is the default

The worker must support a true dry-run mode where:
- All decision logic runs end-to-end (DB reads, scoring, signal generation)
- Telegram alerts fire with explicit `[DRY-RUN]` prefix
- NO orders are placed and NO state is mutated except read-only logs
- Cooldown timers, attempt counters, kill-switch state are NOT touched
  (this is the #134 trap — DRY-RUN mode contaminating live state)

If the worker's flag is named `*_LIVE=true` (opt-in), default = DRY-RUN.
If the worker's flag is named `*_DRY_RUN=true` (opt-out), default = LIVE.
**Prefer opt-in to LIVE** so the safe state is the default.

### 2. Run one full cycle in DRY-RUN against PRODUCTION data

Not a unit test — a real cycle hitting the real exchange APIs, the real
DB, with the worker's real config. Two ways:

```bash
# Option A — manual one-shot
cd ~/baba/wealth-ecosystem/domains/multi_dex_trading_agent
.venv/bin/python3 ../../ops/funding_arb_registry/<worker>.py
# (with the LIVE flag NOT set)
```

```bash
# Option B — temporarily install plist and run once
launchctl load -w <plist-path>
launchctl kickstart -k gui/$(id -u)/<service-name>
# wait for one cycle, check stdout/stderr logs
launchctl unload <plist-path>
```

### 3. Verify DRY-RUN output

Check three things in the worker's stdout / stderr log:

- **a) Decision logic ran for every relevant subject.** E.g. for auto-close,
  every open pair was evaluated. Count subjects in DB vs count in log.
- **b) Telegram alert(s) fired with `[DRY-RUN]` prefix.** Pop open Telegram,
  scroll to BABA Group CoS channel, eyeball the format. If a real-money
  trade SHOULD have fired, you should see a corresponding DRY-RUN message.
- **c) No state mutations escaped.** Inspect:
    - `auto_close_state.json` (cooldowns) — last_attempt_ts unchanged?
    - `auto_open_budget_state.json` — today's notional unchanged?
    - `audit_vs_live_latest.json` — should still be the audit's snapshot, not
      the worker's
    - `pnl_ledger.db` — no new rows from this dry-run cycle

### 4. Address any findings

- If a check failed: fix the bug, GOTO step 2 (re-run DRY-RUN).
- If alerts didn't fire when expected: fix the alert path before LIVE.
- If decisions look wrong: tune thresholds in DRY-RUN before they touch
  capital.

### 5. Flip LIVE only after a clean DRY-RUN cycle

Set the LIVE env var, restart the service, watch the FIRST live cycle in
real time:

```bash
# Set the LIVE flag in the wrapper script's env
vim ~/baba/wealth-ecosystem/ops/<worker_dir>/run_<worker>.sh
# add: export ARB_AUTO_CLOSE_LIVE=true   (or equivalent)

launchctl kickstart -k gui/$(id -u)/<service-name>

# Watch it work
tail -f ~/baba/wealth-ecosystem/ops/<worker_dir>/<worker>_stdout.log
```

If anything in the first live cycle looks off, kill the service immediately:

```bash
launchctl unload <plist-path>
```

then revert the LIVE flag before debugging.

## When this process applies

Mandatory:

- Any new launchd service that places orders, closes positions, sends
  capital, or changes account settings.
- Any non-trivial change (logic, thresholds, new rule) to an existing
  auto-execute worker. "Cosmetic" changes (logging, comments) excluded.
- Phase D auto-open graduation — DRY-RUN one full daily cycle before
  the first $25/leg auto-open fires.
- Strategy control plane build — DRY-RUN before the SQLite-backed
  state replaces the env-var stubs.

Recommended:

- Any change to a workflow that produces Telegram messages users will act
  on (one-tap, alerts).

Not required:

- Read-only workers (reconciler, audit, dashboard generators, intel brief).
- Build/deploy of pure dashboards / digests / reports.

## Trapdoor: DRY-RUN-poisoning

The classic anti-pattern, exemplified by task #134:

```python
# WRONG — DRY-RUN updates the same cooldown table that LIVE reads.
if not LIVE:
    print("dry-run: would close pair X")
mark_cooldown(pair_id)  # ALWAYS RUNS — even in dry-run
```

```python
# RIGHT — DRY-RUN exits before any state mutation.
if not LIVE:
    print("dry-run: would close pair X")
    continue   # do NOT mark cooldown in dry-run
mark_cooldown(pair_id)  # only when actually firing
```

When reviewing a worker's DRY-RUN code path, search for every state
write (DB INSERT/UPDATE, JSON file write, env mutation) and verify it's
inside the `if LIVE` branch.

## Cross-references

- Task #130 — this process lesson
- Task #134 — auto_close_worker DRY-RUN poisoning bug
- Task #146 — realised PnL not recorded (auto_close passed --realized-pnl-usd 0)
- Task #149 — HLP vault model formatting + auto_close close_reason tag
- AUTO_OPEN_GRADUATION_PLAN.md §5 — phased rollout uses DRY-RUN-first
  for every dial-up.
