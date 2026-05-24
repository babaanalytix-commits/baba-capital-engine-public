# Trustless on-chain audit via Blockscout

> Status: v0.5 shipped 2026-05-24. Free, no API key, stdlib-only client.

## Why

The LP pillar holds capital across multiple EVM chains in concentrated-liquidity NFT positions. Internal registries can drift (lost commits, partial fills, half-completed rebalances). Any "what do I own" claim that the engine makes must be re-derivable from on-chain state — otherwise a silent registry bug eats the bankroll.

Blockscout's public REST v2 API provides the read-side ground truth we need. No auth, no key, generous rate limit. The engine ships a thin Python client (`engine/data/blockscout.py`) and an audit script (`tools/lp_onchain_audit.py`) that runs every 30 minutes via launchd.

## What it checks

For each supported chain (Base, Ethereum, Optimism, Arbitrum):

1. **Native gas balance** — alerts when < $0.50 worth and USDC is sitting (risk of stuck txs).
2. **ERC-20 token balances** — focused on USDC, AERO, WETH; spam tokens are tagged and excluded from totals.
3. **NFT holdings** — Aerodrome Slipstream position NFTs (LP positions), Velodrome equivalents on Optimism.
4. **Smart-account detection** — surfaces EIP-7702 / Safe / contract-wallet status (the LP wallet is currently an EIP7702StatelessDeleGator on Base).

It then compares against the prior snapshot and flags:

- 🟡 idle USDC ≥ $5 with no LP position
- 🟡 low gas on a chain with active USDC
- 🔴 LP NFT count changed unexpectedly

## Design choices

- **Stdlib only**: no `requests`, no `web3.py`. Just `urllib` so the audit can't be broken by a transitive dep upgrade in the engine venv.
- **Per-chain Blockscout instances**: each supported chain has its own Blockscout host (`base.blockscout.com`, `eth.blockscout.com`, etc). The client maps `chain_key → host` internally so callers don't need to know.
- **Fail-soft per chain**: a Blockscout outage on Optimism shouldn't block reporting on Base. Chain-level errors are recorded in the snapshot, not raised.
- **Spam-token heuristic**: huge total supply + no market cap + no exchange rate = excluded from USD totals. Airdrop dust does not pollute the audit.
- **Reads-only**: the audit never signs or sends a transaction. All trustless verification, zero attack surface.

## What's NOT covered yet

- **HyperEVM** — Blockscout doesn't index HyperEVM. Roughly $32 USDC sat on HyperEVM per recent snapshots; that's currently audit-silent. Follow-up adapter (`engine/data/hyperevm_explorer.py`) is queued — will use Hyperliquid's own RPC + a small block-explorer surface.
- **Per-NFT stake status** — knowing the NFT exists isn't the same as knowing it's staked in the gauge. Phase 2 will read `gauge.stakedContains(tokenId)` and `nfpm.positions(tokenId)` directly via `read_contract`.
- **Fee accrual** — `nfpm.positions(tokenId)` returns `tokensOwed0/1`; phase 2 will surface unclaimed fees in the audit.

## Operator runbook

Manual audit:

```bash
cd ~/baba/wealth-ecosystem
set -a && . domains/multi_dex_trading_agent/instances/default/.env && set +a
export PYTHONPATH="$PWD"
domains/multi_dex_trading_agent/.venv/bin/python3 tools/lp_onchain_audit.py
```

Or via the engine CLI:

```bash
~/baba/wealth-ecosystem/engine/run.sh lp-onchain-audit
```

Schedule (every 30 minutes):

```bash
launchctl bootstrap gui/$UID \
  ~/baba/wealth-ecosystem/engine/launchd/com.baba.lp-onchain-audit.plist
launchctl kickstart -k gui/$UID/com.baba.lp-onchain-audit
```

Snapshots:

- `engine/_audit_runs/lp_onchain_latest.json` — current state
- `engine/_audit_runs/lp_onchain_history.jsonl` — append-only history
