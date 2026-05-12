# Auto-Open Graduation Plan — CARRY Pairs

**Status:** Logged 2026-05-12. Gated on **72 hours of cumulative operational
uptime** after the 2026-05-12 fix wave, with all clean-day gates green.
Earliest go-live decision: **whenever those 72 uptime-hours complete
cleanly**, not before 2026-05-15.

**Yomi's directive (2026-05-12):**
> Once we fix all these little bugs with funding arbs, next step is to fully
> automate the approval process. Once no bugs are discovered in 3 days,
> audit my thoughts.

Plus (2026-05-12, follow-up):
> There will be periods I switch off my Mac. This should not degrade any
> time lines or qualifications we are making on any of the agents we are
> developing.

This is that audit + plan, with explicit handling of Mac downtime.

---

## 1. The "3 clean days" gate — what it actually means

**Yomi's instinct is right but under-specified.** Before flipping the switch,
we need to define what "clean" means or we'll move a goalpost we didn't know
existed.

**Proposed definition of a clean day:**

1. **Zero unattributed orphans** in `audit_vs_live_latest.json` (the fixed audit).
2. **Zero CARRY-dust above the HL min** ($10) at any point. Sub-$10 dust is
   fine — that's a venue floor, not a bug.
3. **Zero divergence > 15%** between fundamentals.db funding rate and
   `funding_payment_history_v1` ground truth on any GRVT instrument. (This
   needs the reconciliation worker — task #179.)
4. **Zero naked legs** (margin breach without auto-flatten kicking in, or
   auto-flatten failing).
5. **Zero phantom positions** in the dashboard (closed pairs still showing
   live size).
6. **Daily P&L reconciles** to within $0.05 of pnl_ledger.db's prediction.

The **3-day window starts when the funding-rates worker completes one full
cycle on the new code AND the next CARRY pair gets closed using the new
HL ROUND_UP/sweep path**. Not "3 days from today." We need real reps under
the new code, not 3 days of clock time on the old code.

If the reconciliation worker (#179) isn't built yet by then, gate item #3
becomes manual cross-check. That's acceptable for go-live but #179 should
follow within a week.

### 1a. Mac downtime — clock pauses, qualifications hold

The 72h clean-window is measured in **cumulative operational uptime**, not
wall-clock. If the Mac is off for 8 hours overnight, that's 8 hours subtracted
from the elapsed wall-clock when computing how much clean window we've banked.
Three rules govern this:

1. **Downtime is neutral, not a bug.** If a gate (e.g. funding reconciliation)
   cannot be evaluated because the system was off, it's marked `not_evaluated`
   for that interval — NOT `failed`. The clean-window clock stops; it doesn't
   reset.

2. **First-tick-after-downtime is treated as observation, not action.** When
   any agent (auto_close_worker, reconciler, dashboards) comes back online
   after the heartbeat detects a gap > 30 min, that agent runs ONE catch-up
   cycle that:
   - Refreshes all live state (positions, treasury, marks)
   - Writes new snapshots to JSON
   - Does NOT trigger any close, alert, or qualification verdict on that
     first cycle
   The clean-window clock starts ticking again on the SECOND post-resume
   cycle, with fresh data.

3. **Real downtime budget.** Up to 12 hours of downtime in any rolling 72h
   window is normal (sleep, travel). If downtime exceeds 24 hours in a
   rolling 72h, we surface that as a process problem (Yomi away too long
   without remote-Mac or watchdog handoff), and the auto-graduation goes on
   hold until either a) downtime falls back inside budget, OR b) Yomi
   explicitly waives.

**Implementation hooks:**
- `funding_reconciler.py` — detects gaps in `fundamentals.db.funding_rates`
  > 15 min and marks the affected predicted-funding window as
  `gap_detected_minutes`. A pair with > 30 min of prediction gap is flagged
  `not_reconcilable_this_cycle` (treated as not_evaluated, not divergent).
- `auto_close_worker.py` — at startup, if `time.time() - last_run > 30*60`,
  skips time-stop evaluation for ONE tick (catch-up only).
- Heartbeat watchdog already alerts on > 6h offline (#139); will distinguish
  "expected downtime" (manual Mac shutdown) from "service crash" by checking
  whether the entire Mac was off, not just a single service. State persists
  via `state/uptime_log.jsonl` — one line per agent start/stop event.
- Clean-window calculator: a tiny `ops/reporting/clean_window_clock.py`
  reads the uptime log + the audit/reconciler/dust JSONs, computes
  `(banked_clean_hours, downtime_hours, contamination_events)` and surfaces
  on the BABA Capital Engine dashboard as a progress bar toward 72h.

The principle: **the system has no opinion about whether Yomi is at his
desk.** Either the data is there and the gates pass, or it's missing and we
wait. Nothing degrades a qualification just because the Mac was asleep.

---

## 2. What "fully automated" means — the spec

**My audit:** The phrase "automate the approval process" can mean three
different things. Picking the right one matters.

| Option | Behaviour | Risk surface |
|--------|-----------|--------------|
| **A. Silent auto-open** | Scanner finds opportunity → opens both legs → notifies after | Highest — Yomi has no veto |
| **B. Auto-open with delay** | Notifies "opening in 60s, /cancel to abort" → opens unless cancelled | Moderate — small abort window |
| **C. Smart-default approve** | One-tap stays, but the button defaults to ✅ after 5 min if not actively rejected | Lowest — Yomi still in loop |

**Recommendation: B** — auto-open with a 60-second cancel window. Delivers
the speed advantage (don't lose the opportunity while sleeping), keeps
Yomi in the loop without forcing engagement, and the cancel path is the
existing Telegram inline button. If 60s is too aggressive for night-time,
auto-extend to 5 min during 22:00–06:00 Prague.

If Yomi prefers A or C, the rest of this plan still applies — only the
Telegram flow changes.

---

## 3. Hard prerequisites (must ship before go-live)

These are non-negotiable. None of them is "nice to have."

### 3.1 Capital governor
- **Daily $ budget cap:** auto-open MAX $X/day in new notional (default: 30%
  of free margin across HL+GRVT+Pacifica). Once breached, switch to one-tap
  for the rest of the day.
- **Per-asset cap:** never more than $50 in a single asset across all venues.
- **Per-asset-class cap:** Portfolio Manager (P3) gates already enforce this.
  Confirm `dry_run: false` flipped — currently still in shadow mode.
- **Per-pair count cap:** max N concurrent open pairs (default: 12, room for
  rotation without margin stress).

### 3.2 Quality bar (already exists, tighten for auto)
- Min net carry APR after fees + slippage: **+25%** (auto), vs +15% one-tap.
- Min observed spread over last 4h: **+20%** (auto), vs +10% one-tap.
- Both legs must have liquidity > $10K depth at $50 size.
- Asset NOT in `excluded_assets` (delisted / blacklisted).
- Asset NOT in any pair closed within last 6h (cooldown).

### 3.3 Kill switches
- `/pause` Telegram command → no new auto-opens until `/resume`.
- `/pause N` → pause for N hours.
- Auto-pause triggers: 3 consecutive net-negative pair closes; daily realised
  P&L < -$5; any leg liquidation event; treasury free margin < $20 at any
  venue.
- Kill switch state lives in `auto_open_state.json`, read on every scan tick.

### 3.4 Reporting
- After every auto-open: same Telegram message we send today, but prefixed
  with **"AUTO-OPENED — /cancel within 60s to flatten"**.
- Daily 08:00 Prague digest already covers closes; add a new section
  "Auto-opens in last 24h" listing each + outcome.
- Weekly post-mortem: how often did Yomi cancel? If > 30%, the auto thresholds
  are wrong — narrow them.

### 3.5 Reversion path
- One-tap MUST stay live in parallel. If auto produces unexpected opens,
  flip a single env var (`AUTO_OPEN_ENABLED=false`) to revert. No code
  rollback, no service restart.

---

## 4. What I'd ALSO consider (not in your message, but worth flagging)

These are things Yomi didn't name but probably wants to think about.

1. **Asset-class diversification gate at the auto-open level.** Right now PM
   gates the MD agent. Same logic should gate auto-open: don't let auto-open
   put 100% of new capital into TradFi commodities just because they happen
   to dominate the top of the spread ranking.

2. **Time-of-day rules.** The opportunity scanner already runs hourly. Auto-
   open should NOT fire during the 5 minutes around HL funding settlement
   (every hour on the hour) or around GRVT settlement (every 8h at 00/08/16
   UTC) — funding-time prints are noise.

3. **Sleep-time rules.** Yomi sleeps 23:00–07:00 Prague. Question: should auto-
   open fire at 03:00 Prague when he can't react? Three options:
   - Pause auto entirely 23:00–07:00 (loses overnight ops, safest).
   - Reduce per-trade size 50% during sleep window.
   - Allow but require 5-min cancel window (vs 60s daytime).
   Recommendation: **Reduce size 50% + 5-min cancel** during sleep. Weekly
   review will tell us if this is right.

4. **Correlation guard.** If we already have NATGAS hedged, we shouldn't open
   another NATGAS-correlated trade (CL crude oil, etc.) automatically — the
   margin moves together. PM has `correlated_families` for this; confirm
   it's actually active in auto-open path.

5. **Slippage budget.** If actual fill diverges from quoted by > X bps,
   auto-rollback (close the leg that did fill, don't open the second).
   Today's open_pair.py already does atomic rollback — confirm it's the
   path auto-open uses.

6. **Capital allocator priority.** If a fresh opportunity is materially better
   than an open low-EV pair, capital_rotator (#136) should auto-close the
   weak one to free capital for the new one. This already exists; confirm
   it's wired into the auto-open decision tree, not just one-tap.

7. **Black-swan insurance.** Drawdown-halt at -5% of total deployed capital
   in any 24h period: pause auto-open AND auto-close all CARRY pairs to flat
   (preserving capital takes priority over avoiding realised loss). This is
   the equivalent of the trading-floor circuit breaker. Defaults can be
   tuned; the mechanism MUST exist.

8. **A/B against manual.** For the first week of auto, run BOTH modes:
   auto-open fires automatically AND scanner still sends one-tap alerts for
   every same opportunity. If Yomi would have opened a one-tap that auto
   skipped (or vice versa), we capture the disagreement and tune. After 7
   days the one-tap stream becomes opt-in only.

9. **Failure mode drills.** Before flipping auto, simulate: HL down,
   Pacifica down, GRVT down, network partition, signing failure, partial
   fill on one leg, treasury query stale > 10 min. The system must handle
   each gracefully (skip the trade, alert, no naked legs). Test these with
   `--dry-run` flag in Phase D.

10. **Revenue economics.** $0.022/day on the JUP pair is great proof but
    won't move the needle. At what point does the operational risk of auto
    exceed the marginal capture? My answer: when daily auto-open notional
    would cause a single failed atomic rollback to cost > 1 day of net
    capture across the whole portfolio. Today that's roughly $50 notional
    per trade ceiling. Revisit when capital scales 5×.

---

## 5. Phased rollout

**Don't flip a switch — turn a dial.**

| Phase | Trigger | Behaviour | Duration |
|-------|---------|-----------|----------|
| **D-3 to D-1** | Now → 2026-05-15 | One-tap only; dust + funding-rate fixes baking | 3 days |
| **D-day** | 2026-05-15, if clean window held | Flip `AUTO_OPEN_ENABLED=true` with size cap = $25/leg, daily count = 2, sleep-window paused | 7 days |
| **D+7** | 2026-05-22, if no rejected auto-opens + no rollbacks | Raise to $50/leg, daily count = 4, sleep-window 50% size | 14 days |
| **D+21** | 2026-06-05, if Yomi cancel rate < 30% | Full design: $50/leg, daily count = 8, sleep-window normal | Ongoing |
| **Pause / revert** | Any breach of kill-switch criteria | `AUTO_OPEN_ENABLED=false` instantly | — |

Each phase has explicit pass/fail criteria; we don't graduate on calendar
alone.

---

## 6. Concrete next steps (in order)

1. **2026-05-12 (today):** This plan logged. GRVT funding fix + HL ROUND_UP
   already deployed — restart workers per the launchctl commands above.
2. **2026-05-13:** Reconciliation worker (#179) scaffolded — daily compares
   DB funding to venue payment history, alerts on > 15% divergence per asset.
3. **2026-05-14:** Phase D readiness (#138) extended with the kill-switch +
   capital-governor module. Smoke-test in DRY_RUN.
4. **2026-05-15 morning:** Read the audit + reconciliation reports for
   the 72h window. If clean → Yomi flips `AUTO_OPEN_ENABLED=true` himself.
   If NOT clean → list the surfaced bugs, restart the 3-day clock from the
   fix.
5. **D-day to D+7:** Daily 08:00 digest gets an "Auto-opens" section.
   Yomi's review = thumbs-up or thumbs-down each.
6. **D+7 review:** Yomi + this CoS go through the week's auto-trades. Cancel
   rate, P&L vs one-tap counterfactual, any near-misses. Decide: graduate to
   D+7 phase, hold, or revert.

---

## 7. The honest take

This is achievable within a week if no surprises. The biggest unknown is
**not the code — it's whether Yomi will trust auto-open enough to leave it
on**. The one-tap workflow exists today; many users with a one-tap workflow
that works well never graduate to auto, because the marginal speed benefit
doesn't outweigh the loss of feel for what's happening.

If after 2 weeks the auto-open is producing the same trades Yomi would have
approved manually, with the same outcomes, then it's working — and the
operational benefit (sleep, focus on bigger projects) is the real win, not
incremental P&L.

If it's producing trades Yomi would NOT have approved, the thresholds are
wrong — narrow them, don't override. The system's job is to act like Yomi,
not to be smarter than Yomi.

---

*Owner: Yomi (decision) + this CoS (implementation)*
*Linked tasks: #185 (this plan), #86 (Phase D), #138 (readiness gates),
#179 (reconciliation worker), #87 (capital allocator)*
