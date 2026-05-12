# BABA Capital Engine — Architecture

System organised in three concentric rings: **data sources** (read-only ingestion),
**strategy logic** (the three strategy engines), and **operational rails** (treasury,
reporting, kill switches, control plane). Each ring talks to the others only through
JSON snapshots + sqlite databases on disk — no in-process coupling, so any worker
can be restarted independently without dragging the others down.

---

## High-level diagram

```mermaid
flowchart TB
    subgraph venues [Venues - Read & Write]
        HL[Hyperliquid]
        GRVT[GRVT]
        PAC[Pacifica]
        POLY[Polymarket]
    end

    subgraph data [Data Layer - Read-Only Ingestion]
        FR[Funding Rates Worker<br/>every 5 min]
        TS[Treasury Snapshot<br/>every 30 min]
        INTEL[Intel Brief<br/>FRED, Yahoo, RSS, FG]
    end

    subgraph strategies [Strategy Engines]
        CARRY[CARRY<br/>cross-venue funding arb]
        MD[MD<br/>directional perps]
        ORACLE[ORACLE / ATLAS<br/>prediction markets]
    end

    subgraph rails [Operational Rails]
        CP[Control Plane<br/>per-strategy on/off]
        AUDIT[Position Attribution Audit<br/>5 buckets]
        RECON[Reconciliation Worker<br/>predicted vs actual]
        CLOCK[Clean Window Clock<br/>banks 72h uptime]
        ALERT[Telegram Alerter<br/>one-tap approvals]
        WATCH[Heartbeat Watchdog<br/>external observer]
    end

    HL --> FR
    GRVT --> FR
    PAC --> FR
    HL --> TS
    GRVT --> TS
    PAC --> TS

    FR --> CARRY
    FR --> MD
    INTEL --> CARRY
    INTEL --> MD

    CP -.gates.-> CARRY
    CP -.gates.-> MD
    CP -.gates.-> ORACLE

    CARRY --> ALERT
    MD --> ALERT
    ORACLE --> ALERT

    ALERT --> HL
    ALERT --> GRVT
    ALERT --> PAC
    ORACLE --> POLY

    HL --> AUDIT
    GRVT --> AUDIT
    PAC --> AUDIT
    AUDIT --> CLOCK
    RECON --> CLOCK
    CLOCK -.must-be-green.-> AUTOOPEN[Auto-open<br/>D-day gate]

    WATCH -.observes.-> FR
    WATCH -.observes.-> RECON
    WATCH -.observes.-> ALERT
```

---

## Position attribution — five-bucket classifier

Every position observed on any venue gets sorted into exactly one of five buckets.
Only **orphans** fire a Telegram alert; the other four are surfaced visually but
never alarm. Built after a near-miss where dust from a recently-closed CARRY pair
got mis-flagged as an unattributed leg.

```mermaid
flowchart TD
    POS[Live position observed<br/>on any venue] --> Q1{In open<br/>CARRY pair?}
    Q1 -- yes --> HEALTHY[Healthy CARRY pair]
    Q1 -- no --> Q2{In manual<br/>positions JSON?}
    Q2 -- yes --> MANUAL[Manual exception<br/>directional, single-venue]
    Q2 -- no --> Q3{In MD agent's<br/>open positions?}
    Q3 -- yes --> MD[MD directional<br/>SL/TP managed]
    Q3 -- no --> Q4{< $2.50 AND<br/>matches CARRY<br/>closed in last 7d?}
    Q4 -- yes --> DUST[CARRY dust<br/>flatten when convenient]
    Q4 -- no --> ORPHAN[ORPHAN<br/>fires Telegram alert]
```

---

## Auto-open graduation gate

How a one-tap manual approval workflow graduates to fully-automated open. The
three columns are independent — each runs continuously, and they must ALL stay
green for 72 hours of operational uptime before the gate flips.

```mermaid
flowchart LR
    subgraph gates [Six clean-day gates]
        G1[Zero<br/>orphans]
        G2[Zero CARRY-dust<br/>above HL $10 min]
        G3[Zero divergence<br/>greater than 15 percent funding]
        G4[Zero<br/>naked legs]
        G5[Zero<br/>phantom positions]
        G6[Daily P&L<br/>reconciles ±$0.05]
    end

    gates --> CLOCK{All green<br/>simultaneously?}
    CLOCK -- yes --> BANK[Bank elapsed minutes<br/>toward 72h target]
    CLOCK -- one+ red --> RESET[Reset banked hours = 0<br/>log contamination event]
    CLOCK -- evidence missing --> NEUTRAL[Pause clock<br/>record downtime<br/>do not reset]

    BANK --> AT72{Banked hours<br/>greater than 72?}
    AT72 -- yes --> READY[Operator can flip<br/>AUTO_OPEN_ENABLED=true]
    AT72 -- no --> WAIT[Keep banking]

    READY --> PHASE1[D-day: $25 per leg<br/>x 2 trades per day<br/>sleep-window paused]
    PHASE1 -- 7 clean days --> PHASE2[D+7: $50 per leg<br/>x 4 trades per day<br/>sleep-window 50% size]
    PHASE2 -- 14 clean days --> PHASE3[D+21: $50 per leg<br/>x 8 trades per day<br/>full design]
```

---

## Control plane — per-strategy on/off

Designed but deferred to post-graduation. The catalogue (`strategies_catalogue/*.yaml`)
is the single source of truth for what's runnable. Each strategy registers its
metadata, risks, on/off semantics, and supported venues there. Adding a future
strategy (YIELD, AIRDROP, OPTIONS, etc.) is a config change, not a code change.

```mermaid
flowchart LR
    subgraph cat [strategies_catalogue/]
        MDYML[md.yaml]
        CARRYYML[carry.yaml]
        ORACLEYML[oracle.yaml]
        FUTURE1[yield.yaml?]
        FUTURE2[airdrop.yaml?]
    end

    cat --> STATE[strategy_state.sqlite<br/>per user × strategy × venue]
    STATE --> AGENTS[Agents check state<br/>at top of every tick]

    subgraph commands [Telegram surface]
        SLASH1[/strategies]
        SLASH2[/md off carry hl off etc.]
        SLASH3[/killswitch]
    end
    commands --> STATE

    STATE --> AUDIT[strategy_audit_log<br/>every flip with who/when/why]

    AGENTS -- live --> RUN[Execute normally]
    AGENTS -- paper --> LOGONLY[Log decisions<br/>no execution]
    AGENTS -- off --> PAUSE[Block new entries<br/>hold existing]
    AGENTS -- drain --> CLOSE[Block new entries<br/>close on next opportunity]
```
