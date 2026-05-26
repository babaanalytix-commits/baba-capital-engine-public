# CHANGELOG — 2026-05-26

Single-day operator session covering ORACLE AI v1 ship + multiple
post-launch hardening passes. Documented as one entry because the
fixes and the launch are inseparable — the failures BUILT the safety
system.

## Shipped (new capability)

- **ORACLE AI v1** — two-layer AI judge over Polymarket (Gemini Flash
  triage + Claude Sonnet precision). Full architecture in
  `docs/oracle_ai_v1.md`. Cost target $0.50/day total. Currently in
  MANUAL execution mode pending validation.
- **AUTO_VETO execution scaffold** — env-driven mode dispatcher
  (`SHADOW` / `MANUAL` / `AUTO_VETO` / `AUTO_INSTANT`), 5-filter safety
  layer (min confidence, max concurrent, max auto size, daily loss cap,
  cooldown), Telegram Skip + Pause-All buttons. Default OFF until
  signal-to-PnL ratio proven.
- **L4 content bus tier-gated distribution** — Premium subscribers
  receive ORACLE AI signals in Telegram + on the new subscriber web
  dashboard with the same data set. Premium+ adds Claude rationale +
  risk factors. Free tier sees a teaser with upgrade CTA.
- **Public subscriber web app** at `babacapital.app/app` — iPhone-shell
  PWA preview, Gumroad license-key gated, full unlocked detail when
  authed. Phase 1 client-side shape check; Phase 2 backend via
  Cloudflare Pages Function (`/api/verify-license`) calls Gumroad's
  license verify API with `increment_uses_count=false`. Operator
  backdoor via env var.
- **Public BABA App live preview** at `babacapital.app/live` —
  sanitized view of the engine using same JSON feeds. Public sees
  pulse + LP composite + AI verdict feed + aggregate perf. Hidden:
  positions, sizing, capital, trade alerts.
- **System audit harness extension** — per-worker tick-log staleness
  check + boot-time smoke test for stdlib/dep corruption. Caught the
  same class of silence that triggered today's RCA.

## Fixed (RCAs from real incidents)

- **Gemini 2.5 Flash thinking-mode truncation** — model has reasoning
  ON by default; tokens count against response budget before visible
  output. JSON arrays truncated mid-second-element. Fix:
  `thinkingConfig.thinkingBudget=0` + `maxOutputTokens: 4096`. Also
  improved diagnostic logging to capture truncation patterns.
- **First live AI signal lost 50%** — SHORT on "SPX Opens Up or Down
  May 26" entered 4h after the open bell. Claude misread "opens" as
  future-tense; Polymarket was still open in oracle-settlement
  drift. Fixes (both): markets.py extreme-price hard skip
  (yes_price >= 0.97 or <= 0.03) + judge prompt POST-EVENT GUARD
  forcing explicit "when does this resolve" reasoning.
- **SHADOW mode was a label not a gate** — strategy emitted signals
  regardless of `ORACLE_AI_SHADOW=true`; downstream Telegram alerter
  ignored the metadata flag. Universal pattern documented: kill
  switches gate at emission, never at consumption. Fix:
  `if SHADOW_MODE: return []` from strategy.
- **Daemon-cached env vars** — `SHADOW_MODE` was a module-level
  constant set once at import. Env flips required hard restart, not
  just `launchctl kickstart`. Fix: function-call read pattern
  (`_is_shadow_mode()`) so the env hot-reloads per scan.
- **GRVT silent $0 in aggregator** — treasury snapshot returned
  `{ok: False, error: "grvt-pysdk not installed", balance_usd: 0}`
  but unified-state aggregator read `balance_usd` without checking
  `ok` or `error`. Reported `health: GREEN` for a venue with $252
  + 11 positions. Defensive fix: aggregator now treats `ok=False
  OR error != ""` as fetch failure → marks RED with actual error
  surfaced.
- **23h risk-worker silence — ops/email stdlib shadowing** — the
  BMI email automation directory at `ops/email/` shadowed Python's
  stdlib `email` package whenever workers ran with `cwd=ops/`.
  `email.parser` failed to import → workers crashed silently every
  60s. Six false leads (brew Python corruption, venv inheritance,
  PYTHONHOME env pollution, missing deps, plist re-bootstrap, hard
  restart) before finding the actual cause. Fix: rename to
  `ops/email_automation/`.

## New safety nets (so the next silent failure surfaces fast)

- **Boot smoke-test** (`ops/diagnostics/boot_smoke_test.py`) — fail-fast
  stdlib + dep sanity check that every critical worker runs at boot.
  Exits 1 + sends URGENT admin DM on import failure.
- **Tick-log staleness check** (in `ops/diagnostics/system_audit.py`)
  — detects workers that have a launchd PID but haven't written to
  their JSONL tick log in N minutes. Today's 23h silence would have
  been caught within ~30 min if this had existed.
- **AI crazy-trigger** (in `cost_ledger.should_auto_pause()`) —
  fast-acting kill switch BEFORE the slow 200-call ratio gate:
  cumulative 24h loss > $10 → pause; single trade lost > 50% of size
  → pause; spent > $2 in 7d with zero resolved → pause.

## Files changed (public-repo scope)

| File | Type | Why |
|---|---|---|
| `docs/oracle_ai_v1.md` | New | Architecture + safety design for the new AI strategy |
| `docs/CHANGELOG_2026_05_26.md` | New | This document |
| `strategies_catalogue/oracle.yaml` | Update | Added `oracle_ai_v1` entry with full safety + delivery metadata |

Per-file engineering detail (not exhaustive, public-repo doesn't ship
private engine internals) is summarised in `docs/oracle_ai_v1.md`.

## What's NOT in this commit

This repo is the **public-facing architecture + strategy catalogue** for
the BABA Capital Engine. The actual implementation code lives in a
private wealth-ecosystem repo; this changelog summarises commits there.
Private commits in scope today (~30 substantive):

- `engine/strategies/oracle_ai/` (markets.py, triage.py, judge.py,
  strategy.py, cost_ledger.py, exec_mode.py, auto_executor.py)
- `engine/scanner/oracle_scan.py` (publish path + AI badge + AUTO_VETO
  dispatch)
- `engine/content/tiers.yaml` (Premium AI fields)
- `engine/launchd/com.baba.oracle-ai-auto-executor.plist`
- `ops/unified_state/aggregator.py` (GRVT error surfacing)
- `ops/diagnostics/{boot_smoke_test.py, system_audit.py}`
- `ops/email_automation/*` (renamed from ops/email/ + downstream updates)
- `ops/telegram_approval_worker/approval_worker.py` (oracle_skip +
  oracle_pause_all callback handlers)

The babacapital.app website also shipped in parallel:

- `src/pages/live.astro` (rebuilt to iPhone-shell PWA preview)
- `src/pages/app.astro` (subscriber dashboard, license-gated)
- `functions/api/verify-license.js` (Cloudflare Pages Function for
  Gumroad verification)
- `src/pages/index.astro` (ORACLE AI section + live PWA preview cards)
- `src/pages/bmi.astro` (subscriber sign-in callout)
- `src/layouts/BaseLayout.astro` (nav cleanup)

## Grant timeline reference

This is the third week of continuous active development on the public
repo. Previous milestones:

- 2026-05-12 — initial public release (architecture + catalogue + marker contract)
- 2026-05-16 — venue-side SL detection doc
- 2026-05-24 — LP audit + ORACLE macro scanner + TradFi political catalyst (v0.5)
- 2026-05-24 — ORACLE consensus-fade strategy (v0.5.2)
- 2026-05-26 — ORACLE AI v1 + AUTO_VETO + Premium PWA + subscriber app (v0.7)

Cadence target ≥3 substantive commits/week is being maintained.
