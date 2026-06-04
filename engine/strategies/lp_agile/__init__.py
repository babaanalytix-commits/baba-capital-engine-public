"""engine/strategies/lp_agile — concentrated-liquidity LP strategy.

Multi-protocol from Day 1 (per Yomi 2026-05-23):
  - PRJX (HyperEVM)
  - Uniswap V3 (Ethereum)
  - Aerodrome / Slipstream (Base)

Phase 1: subscriber-alert only (no custody, no auto-execute).
Phase 2: auto-execute on a DEDICATED LP wallet — separate from any trading wallet
         (HL / GRVT / Pacifica / Polymarket). See feedback_lp_dedicated_wallet.

Spec: docs/LP_AGILE_SUBSCRIBER_v1_SPEC.md
Strategy id: lp_agile_subscriber_v1
"""

STRATEGY_ID = "lp_agile_subscriber_v1"
SPEC_VERSION = "v1"
