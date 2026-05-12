# Strategy Control Plane — Design Memo

**Logged:** 2026-05-12 (revised same day — Yomi AMT round 2)
**Status:** Designed + scaffolded, NOT built. Build after Auto-Open
Graduation (`AUTO_OPEN_GRADUATION_PLAN.md`) lands cleanly.
**Trigger:** Yomi's AMT, two rounds:

Round 1 (2026-05-12 morning):
> Imagine we have 3 different tools the system can trade with — MD directional
> trades, Funding arbs, Prediction markets. I or a user may want only MD
> directional trades across all perps, so we should be able to switch off
> funding arbs and polymarket, and vice versa. Or a kill switch to stop
> trading all together. Audit my thoughts.

Round 2 (2026-05-12 same session):
> Wherever the selection option/kill switch is located, there should be
> clarity about how the function works and any risks, especially if there
> are open positions, and how to switch back on will work. We should also
> consider, we may probably have more tools and strategies in future, so
> the current list of options may end up being more. AMT and implement
> strategy.

This is the audit + design + extensibility model + the scaffolded help-text
+ explicit phasing.

---

## 1. The verdict on the idea

**Yomi's instinct is correct, and this is a hard requirement, not a polish
item.** Every prop-trading desk that runs more than one strategy needs the
ability to flatten a single strategy without touching the others — and a
master kill for the catastrophic scenario.

For Yomi specifically:
- If MD's risk engine starts misbehaving, you don't want CARRY to die too.
- If a Polymarket protocol upgrade stalls ATLAS, CARRY income shouldn't
  pause.
- For an end-user SaaS later, different users have different risk
  appetites — some will want CARRY only, some will refuse ORACLE.
- Regulatory: certain jurisdictions don't allow prediction-market exposure;
  we need a per-account block.

**What you didn't say but should:** the control plane already exists in
pieces today. It's just scattered:
- `ARB_AUTO_CLOSE_ENABLED` env var (CARRY auto-close)
- `BABA_TRUST_GRVT_FUNDING` env var (CARRY GRVT gate)
- `MD_SKIP_VENUES` env var (MD venue filter)
- `dry_run: true` in `portfolio_targets.yaml` (MD portfolio gate)
- ATLAS policy enable/disable (`*.tuned.yaml`)

The unification is the work — the requirements already exist as scars.

---

## 2. Six considerations to lock down before building

These are decisions that change the design materially. Each needs a
deliberate choice.

### 2.1 Granularity — strategy, venue, or both?
You named **strategy**: MD, CARRY, ORACLE. That's the right primary axis.
But operationally, **venue** comes up often:
- "HL is having API issues — pause everything that touches HL"
- "GRVT margin is critical — don't open new GRVT-leg trades"

**Recommendation:** Two-axis matrix. Strategy × Venue. Default state is
INHERIT from strategy. Venue-level only fires if explicitly set. So
strategy=ON + venue=OFF means trades for that strategy on that venue stop,
but the same strategy on other venues continues.

### 2.2 Existing-positions behaviour when strategy goes OFF
"OFF" can mean three different things, and getting this wrong is dangerous:

| Mode | New entries | Existing positions |
|------|-------------|--------------------|
| **PAUSE** | blocked | held, auto-close still active for risk events |
| **DRAIN** | blocked | auto-close on next normal opportunity (time-stop, spread compression) |
| **FLATTEN** | blocked | close ALL positions immediately, reduce-only market orders |

**Recommendation:** Default OFF = PAUSE. FLATTEN is a separate command
(`/strategy md flatten`) so it can't be triggered by accident.

### 2.3 Where the control surface lives
- **Telegram** — what Yomi uses today. Best for him personally.
- **Web UI** — required for SaaS later. Single-user dashboard with toggles.
- **CLI / env var** — necessary for emergency / launchd / scripts.
- **API** — required for end users to programmatically integrate.

**Recommendation:** Build CLI + Telegram first (covers Yomi's needs). Web UI
when SaaS lands. The single source of truth is the JSON/SQLite state file —
all surfaces are thin clients that read+write that same file.

### 2.4 Failsafe binding (most important)
If the control-plane file is corrupted, missing, or the process can't read
it, what does each agent do?

Two choices:
- **Fail-OPEN:** assume strategies are ON if state can't be read. Continues
  trading. Risky.
- **Fail-CLOSED:** assume strategies are OFF if state can't be read. Pauses
  trading. Safe.

**Recommendation:** Fail-CLOSED. Every agent default-pauses if it can't
read or parse the state. Add a separate health check that alerts via
Telegram if state file is unreadable, so you don't get silent pauses.

### 2.5 Audit trail
Every flip should log:
- Who: Yomi (telegram chat ID), or agent (which one), or CLI (which user),
  or web UI (logged-in user)
- When: ISO timestamp + Unix seconds
- What: strategy, venue (if specified), old state, new state
- Why: required free-text reason field on flips that DISABLE (not on
  enables — those don't need explanation)
- For SaaS: store last 100 flips per user; never delete (retention reqs).

The audit trail is the difference between a control plane and a panic
button. Without it you can't diagnose "wait, who turned MD off at 03:14?"

### 2.6 Single-user vs multi-user (SaaS prep)
For Yomi today: single state file, no auth.
For end users later: per-user rows in SQLite, auth, isolation.

**Recommendation:** Design the schema as if multi-user from day one. Today,
all rows belong to user "yomi". When SaaS arrives, the schema doesn't
change — only the auth layer does. This is ~30 minutes of forethought that
saves a migration later.

---

## 3. Architecture (when we build it)

### 3.1 Source of truth
`ops/control_plane/strategy_state.sqlite`

Schema:
```sql
CREATE TABLE strategy_state (
  user_id      TEXT NOT NULL,           -- "yomi" today; users.id later
  strategy     TEXT NOT NULL,           -- "MD" | "CARRY" | "ORACLE"
  venue        TEXT,                    -- NULL = inherit from strategy; else "HL"|"GRVT"|...
  state        TEXT NOT NULL,           -- "live" | "paper" | "off" | "drain"
  set_at_ts    INTEGER NOT NULL,
  set_by       TEXT NOT NULL,           -- "telegram:<chat_id>" | "cli:<user>" | "agent:<name>"
  reason       TEXT,
  PRIMARY KEY (user_id, strategy, COALESCE(venue, ''))
);

CREATE TABLE strategy_audit_log (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id      TEXT NOT NULL,
  strategy     TEXT NOT NULL,
  venue        TEXT,
  prior_state  TEXT,
  new_state    TEXT,
  set_at_ts    INTEGER NOT NULL,
  set_by       TEXT NOT NULL,
  reason       TEXT
);

CREATE TABLE master_kill (
  user_id      TEXT PRIMARY KEY,
  active       INTEGER NOT NULL,        -- 0 = normal, 1 = killed
  set_at_ts    INTEGER NOT NULL,
  set_by       TEXT NOT NULL,
  reason       TEXT
);
```

### 3.2 Agent integration
Every agent at the top of every tick:
```python
from ops.control_plane import get_strategy_state, MasterKilled

try:
    state = get_strategy_state(user="yomi", strategy="CARRY", venue="hyperliquid")
except MasterKilled:
    log.warning("Master kill switch active — exiting tick")
    return
if state == "off":
    log.info("CARRY/HL is OFF — no new entries this tick")
    # auto-close still runs for risk events (spread, sign, margin) — see 2.2 PAUSE mode
elif state == "drain":
    log.info("CARRY/HL is DRAINING — close on next normal opportunity")
elif state == "paper":
    log.info("CARRY/HL is in PAPER mode — log decisions, do not execute")
elif state == "live":
    pass  # normal flow
```

`get_strategy_state` is fail-CLOSED: returns "off" if it can't read the DB.
Cached for 30 seconds per process so we don't hammer SQLite.

### 3.3 Telegram surface
```
/strategies                  → table view of current state (all strategies × venues)
/md off                      → strategy MD → off (pause new entries; existing held)
/md on                       → strategy MD → live
/md paper                    → MD logs decisions only
/carry hl off                → CARRY on HL only → off
/oracle drain                → ORACLE → drain (close on next opportunity)
/strategy md flatten         → FLATTEN: close ALL MD positions now (typed reason required)
/killswitch on               → master kill, every agent exits next tick
/killswitch off              → master kill cleared (typed confirmation required)
/auditlog                    → last 20 flips
```

Each WRITE command requires the user to be in the allowed-chat-ID list.

### 3.4 Health & observability
- Daily 08:00 digest section: "Control-plane state — what's ON/OFF and why"
- Status dashboard widget: matrix of strategy × venue with state colours
- Telegram alert if any agent fails to read control plane (fail-closed
  pause should never be silent)

---

## 4. What NOT to build (yet)

- **Auto-revert after N hours.** Tempting ("auto-resume MD after 4h pause")
  but adds a foot-gun. If the pause was for a real reason, auto-resume is
  exactly the wrong move. Keep flips manual.
- **Per-asset toggles.** Already exists for MD via PortfolioManager
  `excluded_assets`. Don't duplicate at the control plane.
- **Schedule-based toggles** ("auto-pause MD nights"). Use the existing
  Phase D readiness gate's time windows, not the control plane.
- **Per-pair toggles for CARRY.** Pairs come and go too fast. Use the
  CARRY blacklist + scanner threshold instead.

The control plane is for **strategy- and venue-level coarse control**, not
fine-grained tuning. Resist the urge to make it do everything.

---

## 5. Phasing

Built AFTER Phase D auto-open graduates cleanly (`AUTO_OPEN_GRADUATION_PLAN.md`).
Estimated 2-3 days work once we start.

**Phase 1 (1 day):** SQLite schema, `get_strategy_state` helper, fail-closed
defaults, audit log table. Migrate the existing env-var kill switches to
read from this. No new behaviour — same controls, new home.

**Phase 2 (1 day):** Telegram /strategies + /md|/carry|/oracle commands.
Audit-log writes on every flip.

**Phase 3 (0.5 day):** Status dashboard widget showing the live matrix.

**Phase 4 (0.5 day, deferred):** /killswitch + flatten commands. These have
the highest blast radius — separate phase, separate review.

**Phase 5 (separate workstream — SaaS):** Multi-tenant schema, auth, per-user
audit retention, web UI. Rolls in with the broader BABA Capital Engine
SaaS launch.

---

## 5a. Clarity, risk warnings, and re-enable semantics (AMT round 2)

A toggle is only as useful as the user's understanding of what it does. The
control surface MUST do three things every time someone touches it.

### 5a.1 Inline "what does this actually do?" help

Every control surface (Telegram, dashboard, CLI, future web UI) must show
or expose a plain-English description of the strategy on demand:

- Telegram: `/md help` returns the description, risks, current state,
  current open positions summary.
- Dashboard widget: tooltip / collapsed help block on each strategy row.
- CLI: `python -m control_plane describe md`.

Source of truth: `ops/control_plane/strategies_catalogue/<name>.yaml`
(see §8). Each strategy has `description`, `what_it_trades`, `risks`,
`when_off_means`, `when_on_means` fields. The control surface NEVER
hard-codes this text — it always reads from the YAML.

### 5a.2 Risk-aware confirmation flow when turning OFF

When any flip would DISABLE a strategy (or fire master kill), the surface
MUST show this BEFORE confirming:

```
You are about to switch CARRY → OFF (PAUSE mode).

  Open positions on this strategy:
    • 3 CARRY pairs across hyperliquid+pacifica
    • Total notional: $156.40
    • Unrealised PnL: -$0.04 (delta-neutral, working as designed)
    • Funding accrued today: +$0.18

  What "PAUSE" does:
    ✓ Blocks new pair opens
    ✓ Existing positions HELD (no auto-close from this action)
    ✓ Auto-close worker still fires on risk events (margin, sign-flip)
    ✓ Funding continues to accrue / pay

  What it does NOT do:
    ✗ Does not flatten existing pairs (use FLATTEN command for that)
    ✗ Does not refund or roll fees

  Risk if you forget:
    Pairs continue to consume margin. Funding spread can compress while
    paused — set a phone reminder if you'll be away > 24h.

Confirm? Reply YES + reason in the next message.
```

Two-step confirmation: the user must reply YES with a free-text reason on
the next message. Single-keystroke "yes" is rejected — forces conscious
acknowledgment.

For master killswitch, the summary aggregates across ALL strategies:
"Master kill will pause MD (4 pos, $87) + CARRY (3 pos, $156) + ORACLE (0
pos). Reason required."

### 5a.3 Re-enable semantics — how OFF → ON works

The mistake to avoid: re-enabling auto-fires every signal that would have
been opened during the pause. That creates a queue of stale trades that
ALL hit the venue when you flip back. Hard NO.

Re-enable rules:

1. **Forward-only.** Re-enabling a strategy resumes scanning from NOW.
   Signals that fired during the pause are discarded, not queued.
2. **1-hour grace window.** After a strategy goes OFF→ON, auto-open is
   blocked for 60 minutes. Manual one-tap still works. The grace
   window lets the user check that conditions still match before fresh
   capital deploys.
3. **Re-enable confirmation also requires a reason.** Same two-step flow.
   Audit log captures both the pause reason and the resume reason — useful
   for "why did I turn this back on, what changed?" review.
4. **State of held positions is exactly as left.** A pair paused at age 4h
   resumes at age 4h+pause_duration. The auto_close_worker's catch-up
   gate (#186) handles the time-stop edge case.
5. **From PAUSE → ON: positions resume normal management.** From DRAIN → ON:
   positions stop draining and resume normal management. From FLATTEN
   (which closed everything) → ON: starts from a clean slate.

The Telegram re-enable prompt looks like:

```
You are about to switch CARRY → ON (LIVE).

  Currently held: 3 CARRY pairs ($156.40, paused 14h ago)
  Auto-open: BLOCKED for 60 min after re-enable (grace window)
  Manual one-tap: available immediately

Confirm? Reply YES + "what changed" reason.
```

---

## 6. Why this is deferred

You said "after we have all current systems fully automated" — agree.

The reason isn't hesitation, it's sequencing:
- **Without auto-open running**, the kill switch only saves you from manual
  approvals you can already refuse. Marginal value.
- **With auto-open running**, the kill switch becomes essential — that's
  when "I want MD off but CARRY on" goes from convenience to safety.

Build the thing it's protecting BEFORE you build the protection. Otherwise
the protection has nothing to do and we burn time on a hypothetical.

Logged for D+7 review (after auto-open is observed clean). At that point
this design comes off the shelf and Phase 1 starts.

---

## 7. Future-strategy extensibility (AMT round 2)

Yomi flagged: "We may probably have more tools and strategies in future,
so the current list of options may end up being more."

Correct call. The current set is MD / CARRY / ORACLE — but plausible
additions over the next 12 months include:

- **YIELD** — passive stablecoin yield + LP positions on Pendle/Aave
- **AIRDROP** — points-farming on Pacifica, GRVT, Hyperliquid, Boros
- **ORACLE-2** — prediction-market arbitrage (cross-platform, vs ATLAS
  directional)
- **OPTIONS** — covered-call writing on perp positions (when supported)
- **BORROW** — stablecoin borrow at favorable rates for capital efficiency
- **NFT** / **DEX-MM** — higher-leverage, higher-risk venture strategies

Hard-coding `{MD, CARRY, ORACLE}` anywhere in the control plane code is a
trap. The list MUST be data-driven so adding a strategy is a config change,
not a code change.

### 7.1 Strategy catalogue — single source of truth

`ops/control_plane/strategies_catalogue/<strategy_id>.yaml` — one file per
strategy. Each file defines everything the control plane needs to know:

```yaml
# strategies_catalogue/md.yaml
id: md
display_name: MD — Multi-Dex Directional
status: live                           # live | beta | experimental | deprecated
default_state: paper                   # paper | off | live  (for new users / first deploy)
description: |
  Single-venue directional perpetual trades managed by the strategy
  engine, with stop-loss and take-profit. Trades long OR short on a
  single venue based on technical signals. Not hedged.
what_it_trades:
  - "Long OR short positions on perpetual futures"
  - "One leg only (no hedge)"
  - "SL/TP managed automatically by position manager"
supported_venues: [hyperliquid, grvt, pacifica]
risks:
  - "Directional exposure — full delta to underlying price"
  - "Max loss per trade gated by stop-loss (typically 2% of position)"
  - "Multiple concurrent positions can compound directional bias"
when_off_means: |
  PAUSE: blocks new MD entries; existing MD positions held with their
  SL/TP intact and managed by position_manager. Auto-close still fires
  on risk events.
when_on_means: |
  LIVE: strategy engine generates signals, risk engine filters them,
  portfolio manager enforces diversification, accepted signals execute.
enable_preconditions:
  - "Free margin >= $20 across enabled venues"
  - "MD agent service running (com.baba.trading-agent)"
  - "Portfolio Manager dry_run = false"
adjacent_workers:
  - com.baba.trading-agent
audit_log_category: md
ui_color: "#3a7"                       # for dashboard widget rendering
```

The control plane reads ALL `*.yaml` files in `strategies_catalogue/` at
startup and on every config-reload signal. New strategies appear as soon
as their YAML lands. Removing a YAML file (or marking `status:
deprecated`) hides it from new flips but preserves the audit log.

### 7.2 Strategy registration contract

For a strategy to be controllable, the AGENT THAT IMPLEMENTS IT must:

1. Have a unique `id` matching its YAML filename
2. Read `get_strategy_state(user, strategy_id, venue)` at the top of every
   tick and respect the returned state (live / paper / off / drain)
3. Provide a way to count its own open positions (function reference in
   the YAML — TBD when building)
4. Provide a way to enumerate its own risk events (signal IDs the user
   should know about)

Without these, the strategy YAML may exist but the control plane treats
the strategy as `unimplemented` and grays out its toggle.

### 7.3 What changes when we add a fourth strategy (e.g. YIELD)

1. Drop `strategies_catalogue/yield.yaml` with the metadata
2. The yield agent imports `get_strategy_state(user="yomi", strategy="yield")`
   and respects it
3. Telegram immediately exposes `/yield off`, `/yield help`, etc — no code
   change to the Telegram worker
4. Dashboard widget grows a new row — no code change, it's reading the
   catalogue
5. Master killswitch automatically includes the new strategy

That's the contract. Adding strategies is config, not code.

### 7.4 Naming reservations

To avoid collisions when we eventually onboard end users (SaaS), reserved
strategy IDs:
- Lowercase, snake_case, ≤ 32 chars
- Reserved root namespace: anything in `strategies_catalogue/` is
  Yomi-canonical
- User-defined strategies (SaaS, future): prefixed `u_<userid>_<id>`,
  isolated per-user

---

## 8. Scaffold landed today (2026-05-12)

Even though the control plane build is deferred, the catalogue scaffold
is live so it accumulates accurate metadata as we go (rather than guessing
later).

```
ops/control_plane/
  CONTROL_PLANE_DESIGN.md            ← this file
  strategies_catalogue/
    md.yaml
    carry.yaml
    oracle.yaml
    _SCHEMA.yaml                     ← documents required fields
```

When build starts (post-D+7), Phase 1 reads from this catalogue. When new
strategies emerge between now and then, drop a new YAML and the design is
already complete.

---

*Owner: Yomi (decision) + this CoS (implementation)*
*Status: Deferred until after Auto-Open Graduation completes (earliest 2026-05-22)*
*Linked tasks: #187 (this design), #185 (auto-open graduation), #86 (Phase D)*
