# BABA Capital Engine

**Multi-venue capital engine for crypto perpetuals, funding-rate arbitrage, and prediction markets — public architecture, operational design, and strategy taxonomy.**

This repository documents the design of BABA Capital Engine: a personal trading
infrastructure that runs three independent strategies (CARRY, MD, ORACLE)
across four venues (Hyperliquid, GRVT, Pacifica, Polymarket) with shared
treasury, attribution, risk, and reporting layers. It is not a tutorial.
It's the field notes of a production system, opened up in selected pieces so
the design discipline can be inspected, referenced, and built on.

> Built and operated by [Yomi Oguntona](https://github.com/babaanalytix-commits).
> Public surface: architecture docs, strategy catalogue, runbooks, and an
> on-chain marker contract on Base.

---

## Why this exists

Most public trading-bot repos are one of two things: a single-strategy proof
of concept, or a wrapper around a single venue's API. The interesting work in
real systems happens at the layers in between — strategy taxonomy, attribution,
treasury, position reconciliation, dust prevention, kill switches, graduation
gates, the operational rituals that keep an engine running for months without
a leak.

This repo opens up those layers. The strategy code stays private (it pays
the bills). The patterns are public.

---

## The three strategies

| Strategy | What it trades | Venue model | Status |
|----------|---------------|-------------|--------|
| **CARRY** | Funding-rate arbitrage — matched long/short pairs across two venues | Cross-venue, delta-neutral | Live |
| **MD** | Multi-Dex directional perps with SL/TP managed by the strategy engine | Single-venue, directional | Live |
| **ORACLE** | Prediction-market trading on binary outcomes | Polymarket | Beta (codename ATLAS for v1) |

Each strategy is registered via a YAML in
[`strategies_catalogue/`](./strategies_catalogue/) — the control plane reads
that catalogue at runtime, so adding a new strategy is a config change, not
a code change. See [`docs/CONTROL_PLANE_DESIGN.md`](./docs/CONTROL_PLANE_DESIGN.md)
for the full design.

---

## Repository tour

```
.
├── README.md                           ← you are here
├── LICENSE                             ← MIT
├── docs/
│   ├── architecture.md                 ← system diagrams (Mermaid)
│   ├── CONTROL_PLANE_DESIGN.md         ← per-strategy on/off + master kill
│   │                                     switch design memo (extensibility,
│   │                                     risk-aware confirmation, re-enable
│   │                                     semantics)
│   ├── AUTO_OPEN_GRADUATION_PLAN.md    ← how a one-tap trading workflow
│   │                                     graduates to fully-automated open
│   │                                     with capital governor, kill switches,
│   │                                     phased rollout
│   └── DRY_RUN_FIRST_TICK_DEPLOY.md    ← deploy runbook: every new
│                                         auto-execute worker must run one
│                                         DRY-RUN cycle on production data
│                                         before LIVE
├── strategies_catalogue/
│   ├── _SCHEMA.yaml                    ← documents required strategy fields
│   ├── md.yaml                         ← MD — multi-dex directional
│   ├── carry.yaml                      ← CARRY — funding-rate arbitrage
│   └── oracle.yaml                     ← ORACLE — prediction markets
└── contracts/
    ├── BabaCapitalEngineMarker.sol    ← on-chain authorship marker (Base)
    └── README.md                       ← deploy instructions
```

---

## Design principles

A few things this system commits to that you'll see threaded through the
docs:

**1. Fail-closed defaults.** Every kill switch, every config gate, every
"is the data fresh?" check defaults to OFF / PAUSE / NOT_EVALUATED when
evidence is missing. Failure modes prefer "do nothing" over "do the wrong
thing."

**2. Position attribution before automation.** Before you let a system
auto-open trades, you must be able to look at any open position and answer
"which strategy owns this?" in one glance. Five buckets: CARRY pair, manual
exception, MD directional, CARRY dust, orphan. The audit job runs every 30
min and Telegram-alerts only on the orphan bucket — the other four are
classified silently.

**3. Mac-downtime is neutral.** The clean-window clock counts CUMULATIVE
OPERATIONAL UPTIME, not wall clock. If the machine is off for 8 hours
overnight, the clock pauses — it does NOT reset and it does NOT contaminate.
First tick after wake is observation-only for time-based rules.

**4. Trust venue truth over the local DB.** When the venue UI and the
internal database disagree about funding rates, position sizes, or PnL, the
venue is the ground truth. The local DB has a known class of bugs
(decimal-scale inconsistencies, stale snapshots, etc.) that the venue does
not.

**5. Master/agent separation.** Trading agent wallets MUST be fresh,
isolated keys generated by the venue — never the operator's MetaMask master
key. If the agent leaks, only trading is lost, not custody. This pattern is
non-negotiable; same-key setups have caused real incidents.

---

## What's NOT in this repo

- Strategy logic (entry/exit signals, sizing models)
- Private keys, API tokens, or any credentials
- The internal trading database
- Specific live positions or P&L
- The Telegram approval bot's source

If you're trying to copy-paste a working bot from this, you'll be disappointed.
If you're trying to understand how a production multi-strategy engine is
organised, the docs are the point.

---

## Related work

- **Strategy: ATLAS** — the prediction-markets v1 implementation. Currently
  in paper-trading mode (see `oracle.yaml`).
- **Capital Engine dashboard** — single-page web view, auto-refreshes from
  on-disk JSON snapshots written by each worker.
- **Reconciliation worker** — every 4h, compares funding accrual predicted
  by the local DB against actual venue payment history. Flags >15% divergence
  per leg. Mac-downtime aware (records gap minutes, doesn't false-flag).

These exist in the private codebase; the design considerations behind each
are referenced from the docs in this repo.

---

## On-chain authorship marker

The repo is pinned to an authorship marker contract deployed on Base —
see [`contracts/BabaCapitalEngineMarker.sol`](./contracts/BabaCapitalEngineMarker.sol).

Once deployed, the contract address will be added here:

```
Base mainnet: 0x...  (pending deploy)
```

The contract stores three immutable values: project name, tagline, and
repository URL. It exists to bind the GitHub repo to a Base on-chain
identity — for Builder Rewards scoring and for verifiable authorship.

---

## License

MIT — see [`LICENSE`](./LICENSE). Use freely; this is documentation, not
proprietary IP. The operational discipline patterns generalise to most
trading systems beyond crypto.
