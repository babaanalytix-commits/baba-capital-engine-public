# ORACLE Macro-Event Scanner

> Status: v0.5 shipped 2026-05-24. Matches upcoming US macro releases against active Polymarket markets.

## Why

Polymarket lists binary markets on every major US macro release weeks before each event — Fed rate decisions, CPI prints, jobs reports, GDP, PCE. The liquidity on these markets concentrates in the 7-14 days before the event. Pre-event positioning, given an accurate model of the release, has structural edge.

The scanner closes the discovery loop: instead of browsing polymarket.com daily to find the right markets, the engine auto-matches every Polymarket market against the US macro calendar and surfaces tradeable candidates ranked by liquidity.

## Sources

- **US macro release calendar**: hand-curated 12-month rolling schedule in `engine/data/us_macro_calendar.py`. Source-of-truth dates from Fed, BLS, BEA. Refreshed each January.
- **Polymarket gamma API**: free public endpoint at `https://gamma-api.polymarket.com/markets`, paginated, no auth required.
- **Matching**: per-event keyword list (e.g. FOMC matches `fed`, `fomc`, `rate cut`, `interest rate`) tested against market `question` + `slug`.

## What gets surfaced

For each upcoming event in the next N days (default 30), the top markets by liquidity:

```
[FOMC] FOMC June  ·  2026-06-18 14:00 ET  ·  (24d out)
   • liq=$1,891,578  vol24h=$204,115  resolves 2026-06-17
     ↳ Will the Fed increase interest rates by 50+ bps after the June 2026 meeting?
     ↳ polymarket.com/event/will-the-fed-increase-interest-rates-by-50-bps...
     ↳ matched: fed, interest rate
   • liq=$719,891   vol24h=$190,614  resolves 2026-06-17
     ↳ Will there be no change in Fed interest rates after the June 2026 meeting?
   • liq=$373,776   vol24h=$361,297  resolves 2026-06-17
     ↳ Will the Fed decrease interest rates by 50+ bps after the June 2026 meeting?
```

Output formats:

- Plain-text report (operator console)
- Telegram digest (top 5 candidates by liquidity, posted to `daily-brief` category)
- JSON via the CLI (`--json-only` flag, planned)

## Known v1.1 gaps

- **US-only filter missing**: bare `gdp` keyword matches China GDP markets too. Need to add a country qualifier or exclude non-US tags.
- **`yes_mid_price` always None**: Polymarket gamma's `outcomePrices` field is JSON-encoded inside a list — the v1 decoder doesn't handle that nesting. Fix is one line.
- **Resolve-date proximity filter**: annual buckets (e.g. "Will inflation reach 4% in 2026?") match every CPI event from now through year-end. Pre-event tactical trades want markets that resolve within 7d of the event, not annual buckets.

## Operator runbook

```bash
# One-shot
~/baba/wealth-ecosystem/engine/run.sh oracle-macro-scan --days 30 --min-liquidity 1000

# Scheduled (daily 06:00 UTC = 08:00 Prague morning brief)
launchctl bootstrap gui/$UID \
  ~/baba/wealth-ecosystem/engine/launchd/com.baba.engine-oracle-macro-scan.plist
```

## Future integrations

- Wire `yes_mid_price` into an edge calculator: compare market-implied probability vs (model | consensus) and emit OracleSignal when |edge| > threshold.
- Subscribe to FRED release-actuals (already pulled by `domains/fundamentals/workers/fred_ingester.py`) — auto-resolve hypothesis vs print, log edge, build a track record.
- AI Judge layer (existing in ORACLE pillar) to triage candidates before alerting.
