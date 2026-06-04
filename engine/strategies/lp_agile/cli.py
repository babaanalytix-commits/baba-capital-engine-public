"""engine/strategies/lp_agile/cli.py — LP_AGILE_SUBSCRIBER_v1 operator CLI.

Phase 1 read-only inspection tools. Demonstrates the data + wallet primitives
shipped on Day 1 / Day 2 of the build:

  python -m engine.strategies.lp_agile.cli list-universe
  python -m engine.strategies.lp_agile.cli inspect-pool prjx_hype_usdc
  python -m engine.strategies.lp_agile.cli inspect-wallet 0xYourLPWallet [--protocol prjx]
  python -m engine.strategies.lp_agile.cli capabilities

The wallet inspector reads NFT positions via public eth_call. **No private
key is loaded** — Phase 2 auto-execute lives in a separate executor module
that uses the dedicated LP wallet env vars per
[[feedback-lp-dedicated-wallet]].
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from decimal import Decimal
from typing import Optional

# Import all adapters so they register themselves
import engine.data.lp_pools.prjx          # noqa: F401
import engine.data.lp_pools.uniswap_v3    # noqa: F401
import engine.data.lp_pools.aerodrome     # noqa: F401
from engine.data.lp_pools import get_adapter, list_registered
from engine.strategies.lp_agile.ai_judge import judge_and_annotate
from engine.strategies.lp_agile.alerts import VERDICT_BADGE, render_alert
from engine.strategies.lp_agile.executor import (
    build_mint_preview, render_mint_preview, sign_and_send_mint,
)
from engine.strategies.lp_agile.range_optimizer import compute_range, project_il
from engine.strategies.lp_agile.ranker import rank_pools
from engine.strategies.lp_agile.triggers import evaluate_triggers
from engine.strategies.lp_agile.types import LPAction, Protocol
from engine.strategies.lp_agile.universe import load_pool_universe
from engine.strategies.lp_agile.wallet import read_lp_nft_positions
from engine.strategies.lp_agile.wallet_staked import read_staked_positions


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_list_universe(_args: argparse.Namespace) -> int:
    cfg = load_pool_universe()
    _hr(f"LP universe — {len(cfg.pools)} enabled / {len(cfg.all_pools)} total")
    print(f"Default ranking weights: {cfg.weights}")
    print(f"Freshness windows (s): {cfg.freshness_max_s}\n")

    by_proto: dict[str, list] = {}
    for p in cfg.all_pools:
        by_proto.setdefault(p.protocol.value, []).append(p)

    for proto, pools in sorted(by_proto.items()):
        print(f"\n[{proto}]  ({sum(1 for p in pools if p.enabled)}/{len(pools)} enabled)")
        for p in pools:
            flag = "✅" if p.enabled else "⬜"
            addr = p.pool_address if p.pool_address != "TBD" else "(resolved at runtime)"
            print(f"  {flag} {p.id:32s} {p.pair:18s} fee={p.fee_tier_bps:>3}bps  "
                  f"tvl_min=${p.tvl_usd_min:>11,.0f}  {addr[:46]}")
    return 0


def cmd_inspect_pool(args: argparse.Namespace) -> int:
    cfg = load_pool_universe()
    matches = [p for p in cfg.all_pools if p.id == args.pool_id]
    if not matches:
        print(f"❌ No pool with id={args.pool_id!r} in universe.", file=sys.stderr)
        print(f"   Available: {', '.join(p.id for p in cfg.all_pools)}", file=sys.stderr)
        return 1
    pool = matches[0]
    adapter = get_adapter(pool.protocol)

    _hr(f"INSPECT  {pool.id}  ({pool.pair})")
    print(f"protocol     : {pool.protocol.value}")
    print(f"chain        : {pool.chain.value}")
    print(f"asset class  : {pool.asset_class.value}")
    print(f"fee tier     : {pool.fee_tier_bps} bps ({pool.fee_tier_bps/100}%)")
    print(f"yaml address : {pool.pool_address}")
    resolved = adapter.resolve_pool_address(pool)
    print(f"on-chain addr: {resolved or '— UNRESOLVED —'}")
    print(f"audit status : {pool.audit_status}")
    print(f"airdrop      : {'yes' if pool.airdrop_eligibility else 'no'}")
    if pool.notes:
        print(f"notes        : {pool.notes}")

    _hr("LIVE SNAPSHOT")
    snap = adapter.fetch_snapshot(pool)
    if snap is None:
        print("❌ snapshot fetch failed — adapter returned None.")
        return 2
    print(f"source       : {snap.source.provider} (live={snap.source.is_live}, "
          f"age={snap.source.age_seconds():.1f}s)")
    print(f"base price   : ${snap.base_price_usd:,.6f}")
    print(f"TVL          : ${snap.tvl_usd:,.0f}")
    print(f"volume 24h   : ${snap.volume_24h_usd:,.0f}")
    print(f"7d fees est  : ${snap.fees_7d_usd:,.0f}  (est: 7×24h vol × fee_rate)")
    print(f"FEE APR (est): {float(snap.fee_apr)*100:.2f}%  ← real fees / TVL × 365/7")
    print(f"current tick : {snap.tick_current}")
    print()
    if snap.fee_apr * 100 > Decimal(200):
        print("  ⚠️  APR looks unusually high — likely a high-volume snapshot")
        print("     7d window will smooth this once we replace estimate with real Swap events.")
    return 0


def cmd_inspect_wallet(args: argparse.Namespace) -> int:
    cfg = load_pool_universe()
    wallet = args.wallet
    if not wallet or not wallet.startswith("0x") or len(wallet) != 42:
        print(f"❌ Invalid wallet address: {wallet!r}", file=sys.stderr)
        return 1

    # Determine which adapter(s) to query
    if args.protocol:
        try:
            target_protocols = [Protocol(args.protocol)]
        except ValueError:
            print(f"❌ Unknown protocol {args.protocol!r}. Available: "
                  f"{[p.value for p in list_registered()]}", file=sys.stderr)
            return 1
    else:
        target_protocols = list_registered()

    _hr(f"WALLET  {wallet}")
    print(f"querying {len(target_protocols)} protocol(s): "
          f"{', '.join(p.value for p in target_protocols)}\n")

    total_value_usd = Decimal(0)
    total_fees_usd = Decimal(0)
    found_any = False

    # Load universe once for the staked-position discovery path.
    universe_for_staked = load_pool_universe().all_pools

    for proto in target_protocols:
        adapter = get_adapter(proto)
        print(f"[{proto.value}]  NPM {adapter.position_manager_address}")
        held = read_lp_nft_positions(adapter, wallet)
        staked = read_staked_positions(adapter, wallet, universe_for_staked)
        # De-dup by token_id in case both readers ever surface the same NFT.
        seen: set[int] = set()
        positions = []
        for p in (list(held) + list(staked)):
            if p.token_id in seen:
                continue
            seen.add(p.token_id)
            positions.append(p)
        if not positions:
            print("  (no LP NFTs in this wallet — held OR staked)")
            continue
        found_any = True
        for pos in positions:
            print(f"  • {pos.summary_line()}")
            print(f"      pool:      {pos.pool_address}")
            print(f"      composition: {pos.amount0_human:,.4f} {pos.token0_symbol}  +  "
                  f"{pos.amount1_human:,.4f} {pos.token1_symbol}")
            print(f"      fees owed:  {pos.fees_owed0_human:,.6f} {pos.token0_symbol}  +  "
                  f"{pos.fees_owed1_human:,.6f} {pos.token1_symbol}")
            print(f"      pool now:   1 {pos.token0_symbol} = "
                  f"{pos.pool_price:,.6f} {pos.token1_symbol}  (tick={pos.pool_tick})")
            if pos.position_value_usd is not None:
                total_value_usd += pos.position_value_usd
            if pos.fees_owed_value_usd is not None:
                total_fees_usd += pos.fees_owed_value_usd
        print()

    if found_any:
        _hr("TOTAL")
        print(f"  position value : ${total_value_usd:,.2f}")
        print(f"  fees owed      : ${total_fees_usd:,.4f}")
    else:
        print("\nWallet holds no LP NFTs on any registered protocol.")
        print("(If you expected positions, check you're querying the right wallet —")
        print(" LP positions live on the DEDICATED LP wallet, not your trading wallet.)")
    return 0


def cmd_rank(_args: argparse.Namespace) -> int:
    """Fetch all enabled pools live + composite-rank them."""
    cfg = load_pool_universe()
    _hr(f"RANK — {len(cfg.pools)} enabled pools  (weights: {cfg.weights})")

    snapshots = []
    for pool in cfg.pools:
        adapter = get_adapter(pool.protocol)
        try:
            snap = adapter.fetch_snapshot(pool)
            if snap is not None:
                snapshots.append(snap)
        except Exception as e:                # noqa: BLE001
            print(f"  ❌ {pool.id}: {type(e).__name__}: {e}")
    print(f"\nfetched {len(snapshots)} snapshots → ranking...\n")

    ranked = rank_pools(snapshots, weights=cfg.weights,
                        freshness_max_s=cfg.freshness_max_s)
    if not ranked:
        print("(no pools passed freshness + TVL gates)")
        return 0

    print(f"{'#':>2}  {'pool':36s} {'score':>8} {'fee APR':>9} {'TVL':>14} {'vol 24h':>14}  rationale")
    for i, r in enumerate(ranked, 1):
        s = r.snapshot
        print(
            f"{i:>2}  {s.pool.id:36s} "
            f"{float(r.score):>8.4f} "
            f"{float(s.fee_apr)*100:>8.2f}% "
            f"${float(s.tvl_usd):>13,.0f} "
            f"${float(s.volume_24h_usd):>13,.0f}  "
            f"{r.rationale}"
        )
    print()
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """End-to-end signal generation: rank → triggers → print signals.

    Optional --wallet to consider an actual wallet's open positions when
    computing CLOSE / REBAL / HOLD signals. Without --wallet, only OPEN
    candidate(s) are produced.
    """
    cfg = load_pool_universe()
    _hr("SCAN — rank → triggers → signals")
    snapshots = []
    for pool in cfg.pools:
        try:
            snapshots.append(get_adapter(pool.protocol).fetch_snapshot(pool))
        except Exception as e:                # noqa: BLE001
            print(f"  ❌ {pool.id}: {e}")
    snapshots = [s for s in snapshots if s is not None]
    ranked = rank_pools(snapshots, weights=cfg.weights,
                        freshness_max_s=cfg.freshness_max_s)
    print(f"ranked {len(ranked)} pools")

    open_positions = []
    if args.wallet:
        print(f"reading wallet {args.wallet} for open LP positions (held + staked)...")
        for proto in list_registered():
            try:
                adapter = get_adapter(proto)
                held = read_lp_nft_positions(adapter, args.wallet)
                staked = read_staked_positions(adapter, args.wallet, cfg.all_pools)
                seen = set()
                positions = []
                for p in (list(held) + list(staked)):
                    if p.token_id in seen:
                        continue
                    seen.add(p.token_id)
                    positions.append(p)
                # Map LPNFTPosition → LPPosition for triggers.evaluate
                for p in positions:
                    matching_pool_def = next(
                        (pl for pl in cfg.all_pools
                         if pl.pool_address.lower() == p.pool_address.lower()),
                        None,
                    )
                    if matching_pool_def is None:
                        print(f"  ⚠️  wallet has NFT in unregistered pool {p.pool_address}")
                        continue
                    from engine.strategies.lp_agile.types import LPPosition
                    open_positions.append(LPPosition(
                        position_id=f"nft-{p.token_id}",
                        subscriber_id="self",
                        pool=matching_pool_def,
                        opened_at=p.fetched_at,
                        opened_via_signal_id="manual",
                        range_low_price=p.price_lower,
                        range_high_price=p.price_upper,
                        suggested_capital_usd=p.position_value_usd or Decimal(0),
                        lp_nft_token_id=str(p.token_id),
                    ))
            except Exception as e:            # noqa: BLE001
                print(f"  ⚠️  {proto.value}: {e}")
        print(f"found {len(open_positions)} open LP position(s)")

    signals = evaluate_triggers(ranked, open_positions=open_positions)
    if not signals:
        print("\n(no signals generated — universe might be empty)")
        return 0

    # Run AI judge on every signal (gemini key auto-detected via GEMINI_API_KEY)
    print(f"\nrunning AI judge on {len(signals)} signal(s)...")
    judged = [judge_and_annotate(s) for s in signals]

    # Summary stats
    from collections import Counter
    verdicts = Counter(s.ai_judge_verdict for s in judged)
    print(f"verdicts: {dict(verdicts)}")

    # Group by verdict for cleaner output
    blocked = [s for s in judged if s.ai_judge_verdict == "BLOCKED"]
    watch = [s for s in judged if s.ai_judge_verdict == "WATCH"]
    pass_signals = [s for s in judged if s.ai_judge_verdict == "PASS"]
    other = [s for s in judged
             if s.ai_judge_verdict not in ("PASS", "WATCH", "BLOCKED")]

    if args.format == "alert":
        for group_label, group in (("🚫 BLOCKED", blocked),
                                    ("⚠ WATCH", watch),
                                    ("✅ PASS", pass_signals),
                                    ("· OTHER", other)):
            if not group:
                continue
            print(f"\n{'='*78}\n  {group_label}  ({len(group)} signal{'s' if len(group)!=1 else ''})\n{'='*78}")
            for sig in group:
                print()
                print(render_alert(sig, mode="plain_text"))
                print()
    else:
        print(f"\n=== {len(judged)} SIGNAL(S) (judged) ===\n")
        for sig in judged:
            emoji = {
                LPAction.OPEN: "💰",
                LPAction.CLOSE: "🔴",
                LPAction.REBALANCE: "🟡",
                LPAction.HOLD: "🟢",
            }.get(sig.action, "·")
            badge = VERDICT_BADGE.get(sig.ai_judge_verdict, "·")
            print(f"{emoji} {sig.action.value.upper():9s} {sig.pool.id}  "
                  f"{badge} {sig.ai_judge_verdict} ({sig.ai_judge_tier})")
            print(f"   rationale: {sig.rationale}")
            if sig.action == LPAction.OPEN:
                print(f"   range:     ${sig.range_low_price:,.4f} → "
                      f"${sig.range_high_price:,.4f}  ({sig.range_label})")
                print(f"   suggest:   {float(sig.suggested_capital_pct_of_lp_bankroll)*100:.0f}% of LP bankroll")
                print(f"   yield:     ${float(sig.expected_daily_fee_usd_per_1k):.4f}/day per $1K")
                print(f"   IL proj:   {sig.il_projection}")
                print(f"   ⚠️ Phase-1 reminder: use a DEDICATED LP wallet, NOT your trading wallet.")
            elif sig.action in (LPAction.CLOSE, LPAction.REBALANCE):
                print(f"   reason:    {sig.reason_code}")
                if sig.alternative_pool_id:
                    print(f"   pivot to:  {sig.alternative_pool_id}")
            print(f"   judge:     {sig.ai_judge_reasoning}")
            print()
    return 0


def cmd_dry_run_open(args: argparse.Namespace) -> int:
    """Build + display a mint preview for the top-ranked pool. NEVER sends.

    Workflow:
      1. Scan + rank as normal
      2. Run AI judge on the top OPEN signal
      3. Build the mint calldata + preview the params
      4. Display — including pre-conditions (swap needed? approve needed?)

    Output ends with the calldata that WOULD be sent if you tapped APPROVE.
    Review it carefully before any live execution.
    """
    from decimal import Decimal as _D
    cfg = load_pool_universe()
    _hr("DRY-RUN OPEN — build mint preview for top pool")

    snapshots = []
    for pool in cfg.pools:
        try:
            snapshots.append(get_adapter(pool.protocol).fetch_snapshot(pool))
        except Exception as e:                # noqa: BLE001
            print(f"  ❌ {pool.id}: {e}")
    snapshots = [s for s in snapshots if s is not None]
    ranked = rank_pools(snapshots, weights=cfg.weights,
                        freshness_max_s=cfg.freshness_max_s)
    if not ranked:
        print("\n❌ No ranked pools — universe might be stale.")
        return 2

    signals = evaluate_triggers(ranked)
    open_signals = [s for s in signals if s.action == LPAction.OPEN]
    if not open_signals:
        print("\n(no OPEN signals — wallet already has positions, or no ranked pool above thresholds)")
        return 0
    judged = [judge_and_annotate(s) for s in open_signals]
    # --pool override (task #100b): caller can target a specific pool id
    # instead of letting the ranker pick. Useful when allocating across
    # tiers and the operator wants to override the auto-top-pick.
    pool_override = getattr(args, "pool", None)
    if pool_override:
        sig = next((s for s in judged if s.pool.id == pool_override), None)
        if sig is None:
            print(f"\n❌ --pool '{pool_override}' not in current OPEN signals. "
                  f"Available: {[s.pool.id for s in judged]}")
            return 2
    else:
        # Pick the first non-BLOCKED OPEN
        sig = next((s for s in judged if s.ai_judge_verdict != "BLOCKED"), None)
    if sig is None:
        print("\n🚫 All OPEN signals BLOCKED by AI judge — would not mint:")
        for s in judged:
            print(f"  - {s.pool.id}  {s.ai_judge_verdict}  {s.ai_judge_reasoning}")
        return 0

    print(f"\nTop OPEN signal: {sig.pool.id}  verdict={sig.ai_judge_verdict}")
    print(f"  judge ({sig.ai_judge_tier}): {sig.ai_judge_reasoning}")

    # Override size from CLI if provided
    size = _D(str(args.size)) if args.size else None
    try:
        preview = build_mint_preview(sig, target_position_usd=size)
    except NotImplementedError as e:
        print(f"\n⚠️  {e}")
        print("   Try a Slipstream pool in v0.1, or wait for PRJX/UniV3 executor.")
        return 0
    except Exception as e:                    # noqa: BLE001
        print(f"\n❌ Preview build failed: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1

    print(render_mint_preview(preview))
    print("Next step: if pre-conditions are met, the LIVE mint command would sign")
    print("the calldata above with your LP_WALLET_PRIVATE_KEY and send it. We're")
    print("NOT shipping that command until you've reviewed this preview and the")
    print("one-tap Telegram path is wired. NO MONEY MOVED.")
    return 0


def cmd_live_open(args: argparse.Namespace) -> int:
    """LIVE: sign + send mint for top-ranked OPEN signal. Requires --confirm.

    Same flow as dry-run-open but actually executes after a final yes/no prompt.
    NEVER moves money without:
      1. --confirm flag on the command
      2. typed 'yes' at the in-CLI confirmation prompt (when --no-prompt absent)
    """
    from decimal import Decimal as _D
    from engine.strategies.lp_agile.env import get_lp_config

    cfg = load_pool_universe()
    _hr("LIVE-OPEN — sign + send mint")

    # 1. Pre-flight env config check
    try:
        lpcfg = get_lp_config()
    except Exception as e:                    # noqa: BLE001
        print(f"\n❌ .env.lp load failed: {e}")
        return 1
    if not lpcfg["pk_validated"]:
        print(f"\n❌ LP_WALLET_PRIVATE_KEY missing or does not derive to "
              f"{lpcfg['wallet_address']}. Fix .env.lp first.")
        return 1

    # 2. Scan + rank + trigger + judge (same as dry-run)
    snapshots = []
    for pool in cfg.pools:
        try:
            snapshots.append(get_adapter(pool.protocol).fetch_snapshot(pool))
        except Exception as e:                # noqa: BLE001
            print(f"  ⚠️  {pool.id}: {e}")
    snapshots = [s for s in snapshots if s is not None]
    ranked = rank_pools(snapshots, weights=cfg.weights,
                        freshness_max_s=cfg.freshness_max_s)
    if not ranked:
        print("\n❌ no ranked pools (freshness/TVL gates excluded all)")
        return 2

    signals = evaluate_triggers(ranked)
    open_signals = [s for s in signals if s.action == LPAction.OPEN]
    if not open_signals:
        print("\n(no OPEN signals — wallet may already hold positions or no ranked pool above thresholds)")
        return 0
    judged = [judge_and_annotate(s) for s in open_signals]
    sig = next((s for s in judged if s.ai_judge_verdict != "BLOCKED"), None)
    if sig is None:
        print("\n🚫 all OPEN signals BLOCKED by AI judge — refusing live-open")
        for s in judged:
            print(f"  - {s.pool.id}  {s.ai_judge_verdict}  {s.ai_judge_reasoning}")
        return 0

    print(f"\nTop OPEN: {sig.pool.id}  verdict={sig.ai_judge_verdict} ({sig.ai_judge_tier})")
    print(f"  judge: {sig.ai_judge_reasoning}")

    # 3. Build preview + show it
    size = _D(str(args.size)) if args.size else None
    slip = _D(str(args.slippage))
    try:
        preview = build_mint_preview(sig, target_position_usd=size, slippage_pct=slip)
    except Exception as e:                    # noqa: BLE001
        print(f"\n❌ preview build failed: {type(e).__name__}: {e}")
        return 1
    print(render_mint_preview(preview))

    # 4. Confirmation gates
    if not args.confirm:
        print("⛔ NOT EXECUTING — re-run with --confirm to actually sign + send.")
        return 0

    blocking_pcs = [pc for pc in preview.preconditions
                    if pc.startswith("INSUFFICIENT") or pc.startswith("LOW GAS")]
    if blocking_pcs:
        print("\n🚫 BLOCKING pre-conditions present — refusing live-open:")
        for pc in blocking_pcs:
            print(f"  ✗ {pc}")
        return 2

    if not args.no_prompt:
        print()
        print(f"🚨 About to LIVE-MINT a {preview.pool.id} position of ${preview.target_position_usd}")
        print(f"   from wallet {preview.recipient}")
        print(f"   (~${preview.est_round_trip_cost_usd:.2f} estimated round-trip cost,")
        print(f"    {preview.breakeven_days_at_200apr:.1f} days to break even at 200% APR)")
        try:
            answer = input("   Type 'yes' to proceed, anything else to abort: ").strip().lower()
        except EOFError:
            answer = ""
        if answer != "yes":
            print("⛔ ABORTED by user.")
            return 0

    # 5. SEND IT
    # 2026-05-29 (#42): auto_swap defaults to True so insufficient-balance
    # cases self-heal via swap.py rather than failing. Env override
    # LP_AUTO_SWAP=false reverts to the conservative mode where the open
    # command fails on insufficient token0/token1 and the operator swaps
    # manually first. LP_SWAP_SLIPPAGE_PCT is a percent value (e.g. "1.0"
    # = 1%) — converted to fraction (Decimal("0.01")) before passing.
    import os as _os
    auto_swap_enabled = (
        _os.environ.get("LP_AUTO_SWAP", "true").lower() != "false"
    )
    swap_slip_pct_env = float(
        _os.environ.get("LP_SWAP_SLIPPAGE_PCT", "1.0")
    )
    # env is percent; function wants fraction
    swap_slip_fraction = Decimal(str(swap_slip_pct_env / 100.0))
    print(
        "\n📡 Signing + sending — do not Ctrl+C, takes ~30-90 sec for 3 confirmations..."
    )
    if auto_swap_enabled:
        print(f"   (auto_swap=ON, swap slippage {swap_slip_pct_env}%)")
    result = sign_and_send_mint(
        sig, target_position_usd=size, slippage_pct=slip,
        auto_swap=auto_swap_enabled,
        swap_slippage_pct=swap_slip_fraction,
    )

    print(f"\n{'='*78}")
    print(f"  RESULT (took {result.duration_s:.1f}s)")
    print(f"{'='*78}")
    if result.success:
        verify_emoji = "✅" if result.nft_verified_on_chain else "⚠️"
        print(f"  ✅ MINT SUCCEEDED")
        print(f"  NFT tokenId         : {result.nft_token_id}")
        print(f"  approve {preview.token0_symbol} tx  : {result.approve0_tx_hash}")
        print(f"  approve {preview.token1_symbol} tx : {result.approve1_tx_hash}")
        print(f"  mint tx             : {result.mint_tx_hash}")
        print(f"  {verify_emoji} Trustless verify    : NFT in wallet = {result.nft_verified_on_chain}")
        print(f"\n  Explorer:")
        print(f"    https://basescan.org/tx/{result.mint_tx_hash}")
        if result.nft_token_id:
            print(f"    https://basescan.org/token/{preview.npm_address}?a={result.nft_token_id}")
    else:
        print(f"  ❌ FAILED: {result.error}")
        if result.approve0_tx_hash:
            print(f"  approve {preview.token0_symbol}: {result.approve0_tx_hash}")
        if result.approve1_tx_hash:
            print(f"  approve {preview.token1_symbol}: {result.approve1_tx_hash}")
        if result.mint_tx_hash:
            print(f"  mint tx (failed):      {result.mint_tx_hash}")
            print(f"    https://basescan.org/tx/{result.mint_tx_hash}")
    print(f"{'='*78}\n")
    return 0 if result.success else 3


def cmd_report(_args: argparse.Namespace) -> int:
    """Show net P&L per LP position from the cost ledger."""
    from engine.strategies.lp_agile.cost_ledger import summarize_all_positions
    _hr("LP COST LEDGER — net P&L per position")
    summaries = summarize_all_positions()
    if not summaries:
        print("\n(no LP positions yet — ledger empty. First mint creates it.)")
        return 0
    print(f"\n{'position':>40s}  {'gas':>7s}  {'other':>7s}  {'benefit':>8s}  {'NET P&L':>9s}  events  opened")
    print("-" * 105)
    total_gas = total_other = total_benefit = total_net = 0
    from decimal import Decimal as _D
    for s in summaries:
        if s is None:
            continue
        print(f"{s.position_id:>40s}  "
              f"${float(s.total_gas_usd):>6.2f}  "
              f"${float(s.total_other_costs_usd):>6.2f}  "
              f"${float(s.total_benefits_usd):>7.2f}  "
              f"${float(s.net_pnl_usd):>+8.2f}  "
              f"{s.n_events:>6}  "
              f"{(s.open_timestamp or '')[:16]}")
        total_gas += float(s.total_gas_usd)
        total_other += float(s.total_other_costs_usd)
        total_benefit += float(s.total_benefits_usd)
        total_net += float(s.net_pnl_usd)
    print("-" * 105)
    print(f"{'TOTAL':>40s}  ${total_gas:>6.2f}  ${total_other:>6.2f}  "
          f"${total_benefit:>7.2f}  ${total_net:>+8.2f}")
    return 0


def cmd_pwa_snapshot(_args: argparse.Namespace) -> int:
    """Write lp_agile_latest.json for the PWA LP card to read."""
    from engine.strategies.lp_agile.pwa_snapshot import write_snapshot, SNAPSHOT_PATH
    _hr("PWA SNAPSHOT — writing lp_agile_latest.json")
    snap = write_snapshot()
    print(f"\n✅ wrote {SNAPSHOT_PATH}")
    print(f"  ranked_pools     : {len(snap['ranked_pools'])}")
    print(f"  open_positions   : {len(snap['open_positions'])}")
    print(f"  ledger n_positions: {snap['ledger_summary']['n_positions_ever']}")
    print(f"  ledger net P&L   : ${snap['ledger_summary']['total_net_pnl_usd']:.2f}")
    if snap['ranked_pools']:
        top = snap['ranked_pools'][0]
        print(f"\n  top pool: {top['pair']} ({top['protocol']}) "
              f"{top['fee_apr_pct']:.1f}% APR  verdict={top.get('verdict') or '—'}")
    return 0


def cmd_check_stake(args: argparse.Namespace) -> int:
    """Trustless read of staking status for an LP NFT."""
    from engine.strategies.lp_agile.gauge_stake import check_stake_status
    _hr(f"CHECK STAKE — tokenId {args.token_id}")
    status = check_stake_status(args.token_id, args.pool_address, args.wallet)
    print(f"  NFT owner          : {status.nft_owner}")
    print(f"  Pool               : {status.pool_address}")
    print(f"  Gauge              : {status.gauge_address or '(no gauge)'}")
    print(f"  Is staked          : {'✅ YES' if status.is_staked else '❌ NO'}")
    if status.is_staked:
        print(f"  Earned AERO        : {status.earned_aero:.6f}  (≈ ${status.earned_aero_usd:.4f})")
        print(f"\n  To claim + unstake : python -m engine.strategies.lp_agile.cli unstake-position "
              f"{args.token_id} {args.pool_address} --confirm")
    else:
        print(f"\n  To stake (earn AERO emissions):")
        print(f"    python -m engine.strategies.lp_agile.cli stake-position "
              f"{args.token_id} {args.pool_address} --confirm")
    return 0


def cmd_stake_position(args: argparse.Namespace) -> int:
    """Stake an LP NFT in its gauge to start earning AERO emissions."""
    from engine.strategies.lp_agile.gauge_stake import stake_nft
    _hr(f"STAKE NFT — tokenId {args.token_id}")
    result = stake_nft(args.token_id, args.pool_address, dry_run=not args.confirm)
    if result.get("ok") and result.get("skipped"):
        print(f"  ⏭️  {result['skipped']} — gauge={result.get('gauge')}")
        return 0
    if not result.get("ok"):
        print(f"  ❌ FAILED: {result.get('error')}")
        for k, v in result.items():
            if k not in ("ok", "error"):
                print(f"    {k}: {v}")
        return 1
    plan = result.get("plan")
    if plan and plan.get("mode") == "dry_run":
        print("  ⚠️  DRY-RUN ONLY — rerun with --confirm to send")
        for k, v in plan.items():
            print(f"    {k}: {v}")
        return 0
    print("  ✅ STAKED")
    for k in ("approve_tx", "deposit_tx", "verified_staked"):
        if k in result:
            print(f"    {k}: {result[k]}")
    return 0


def cmd_unstake_position(args: argparse.Namespace) -> int:
    """Unstake an LP NFT from its gauge (claims AERO first, no time lock)."""
    from engine.strategies.lp_agile.gauge_stake import unstake_nft
    _hr(f"UNSTAKE NFT — tokenId {args.token_id}")
    result = unstake_nft(
        args.token_id, args.pool_address,
        claim_rewards=not args.no_claim, dry_run=not args.confirm,
    )
    if result.get("skipped"):
        print(f"  ⏭️  {result['skipped']}")
        return 0
    if not result.get("ok"):
        print(f"  ❌ FAILED: {result.get('error')}")
        return 1
    plan = result.get("plan")
    if plan and plan.get("mode") == "dry_run":
        print("  ⚠️  DRY-RUN ONLY — rerun with --confirm to send")
        for k, v in plan.items():
            print(f"    {k}: {v}")
        return 0
    print("  ✅ UNSTAKED")
    for k in ("claim_tx", "claimed_aero", "withdraw_tx", "verified_unstaked"):
        if k in result:
            print(f"    {k}: {result[k]}")
    return 0


def cmd_capabilities(_args: argparse.Namespace) -> int:
    """Surface what LP_AGILE_SUBSCRIBER_v1 can and cannot do right now."""
    _hr("LP_AGILE_SUBSCRIBER_v1 — what we can do today")

    print("\n📖 READ (live on-chain + DexScreener cross-check):")
    print("  ✅ Resolve pool address via factory (PRJX / HyperSwap V3)")
    print("  ✅ Fetch pool: current price, TVL, 24h volume, fee APR estimate")
    print("  ✅ Read any wallet's LP NFT positions (range, liquidity, fees owed)")
    print("  ✅ Compute position $ value + uncollected $ fees")
    print("  ✅ Detect in/out-of-range status")
    print("  ✅ Cross-verify on-chain TVL vs DexScreener (drift alert >5%)")

    print("\n📈 RANK + ALERT:")
    print("  ✅ Composite ranking across pools (fee APR + airdrop − IL − depth)")
    print("  ✅ Range optimiser (±2σ balanced, ±1σ aggressive)")
    print("  ✅ Triggers: OPEN / CLOSE / REBALANCE / HOLD (silence-as-feature)")
    print("  ✅ AI judge (Tier 1 rule → Tier 2 Gemini PASS/WATCH/BLOCKED gate)")
    print("  ✅ Subscriber-facing alert formatter (OPEN/CLOSE/REBAL/HOLD)")
    print("  ⬜ Tier 3 Claude shadow-A/B (gated by spending env flag)")

    print("\n💸 EXECUTE (Phase 2 — out of v1 scope):")
    print("  ⬜ Mint LP NFT position (open)")
    print("  ⬜ Decrease liquidity + collect (close)")
    print("  ⬜ Rebalance: close-then-open with new ticks")
    print("  ⬜ Auto-collect fees")
    print("  🔒 Requires DEDICATED LP wallet env vars per")
    print("     feedback_lp_dedicated_wallet (never reuses trading keys)")

    print("\n🎯 SUBSCRIBER PROMISE (Phase 1 ship gate, Day 7):")
    print("  ⬜ Backtest replay 30d on HYPE/USDC, ETH/USDC, AERO/USDC")
    print("  ⬜ Ship gate: net P&L > naive hold by ≥15%")
    print("  ⬜ BMI Premium digest section: OPEN/CLOSE/REBAL/HOLD alerts")
    print("  ⬜ Every OPEN alert carries: 'use dedicated LP wallet' guidance")

    print("\n🔗 REGISTERED ADAPTERS:")
    for proto in list_registered():
        adapter = get_adapter(proto)
        print(f"  • {proto.value:12s} chain={adapter.chain.value:10s} "
              f"NPM={adapter.position_manager_address}")

    if not list_registered() or len(list_registered()) < 3:
        print("\n  ⏭️  Uniswap V3 + Aerodrome adapters land Day 2 (remaining).")
    return 0


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lp_agile",
        description="LP_AGILE_SUBSCRIBER_v1 operator CLI (Phase 1 read-only)",
    )
    parser.add_argument("--log-level", default="WARNING",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list-universe",
                            help="list every pool in lp_universe.yaml")
    p_list.set_defaults(func=cmd_list_universe)

    p_pool = sub.add_parser("inspect-pool",
                            help="live snapshot of a pool by id")
    p_pool.add_argument("pool_id", help="e.g. prjx_hype_usdc")
    p_pool.set_defaults(func=cmd_inspect_pool)

    p_wallet = sub.add_parser("inspect-wallet",
                              help="list LP NFTs held by a wallet (read-only)")
    p_wallet.add_argument("wallet", help="0x-prefixed EVM address")
    p_wallet.add_argument("--protocol",
                          help="limit to one protocol (prjx/uniswap_v3/slipstream)")
    p_wallet.set_defaults(func=cmd_inspect_wallet)

    p_cap = sub.add_parser("capabilities",
                           help="show what the strategy can / cannot do today")
    p_cap.set_defaults(func=cmd_capabilities)

    p_rank = sub.add_parser("rank",
                            help="composite-rank every enabled pool (live)")
    p_rank.set_defaults(func=cmd_rank)

    p_scan = sub.add_parser("scan",
                            help="end-to-end scan: rank → triggers → AI judge → signals")
    p_scan.add_argument("--wallet",
                        help="optional EVM address — generates CLOSE/REBAL/HOLD "
                             "for that wallet's open positions")
    p_scan.add_argument("--format", choices=["summary", "alert"], default="summary",
                        help="summary = one-line per signal; alert = full subscriber-facing format")
    p_scan.set_defaults(func=cmd_scan)

    p_dry = sub.add_parser("dry-run-open",
                           help="build + display mint preview for top pool. NEVER sends.")
    p_dry.add_argument("--size", type=float,
                       help="override LP_PER_POSITION_MAX_USD for this preview")
    p_dry.add_argument("--pool", type=str, default=None,
                       help="target a specific pool id from lp_universe.yaml "
                            "(overrides the top-ranked auto-pick)")
    p_dry.set_defaults(func=cmd_dry_run_open)

    p_live = sub.add_parser("live-open",
                            help="LIVE: sign + send approve+mint for top pool. Requires --confirm.")
    p_live.add_argument("--size", type=float,
                        help="override LP_PER_POSITION_MAX_USD for this trade")
    p_live.add_argument("--pool", type=str, default=None,
                        help="target a specific pool id from lp_universe.yaml "
                             "(overrides the top-ranked auto-pick)")
    p_live.add_argument("--confirm", action="store_true",
                        help="REQUIRED. Without this flag, runs as dry-preview only.")
    p_live.add_argument("--no-prompt", action="store_true",
                        help="skip the interactive 'yes' prompt (use only in automated launchd jobs).")
    p_live.add_argument("--slippage", type=float, default=0.05,
                        help="amountMin slippage tolerance (default 0.05 = 5%%). "
                             "Bump to 0.10-0.20 for asymmetric / first-trade positions.")
    p_live.set_defaults(func=cmd_live_open)

    p_report = sub.add_parser("report",
                              help="net P&L per LP position from cost ledger")
    p_report.set_defaults(func=cmd_report)

    p_snap = sub.add_parser("pwa-snapshot",
                            help="write lp_agile_latest.json for PWA card consumption")
    p_snap.set_defaults(func=cmd_pwa_snapshot)

    p_chk = sub.add_parser("check-stake",
                           help="trustless read: is LP NFT staked in gauge? earned AERO?")
    p_chk.add_argument("token_id", type=int, help="NFT tokenId (e.g. 71276872)")
    p_chk.add_argument("pool_address", help="0x-prefixed pool address")
    p_chk.add_argument("wallet", help="LP wallet that holds / staked the NFT")
    p_chk.set_defaults(func=cmd_check_stake)

    p_stake = sub.add_parser("stake-position",
                             help="stake LP NFT in gauge to start earning AERO emissions")
    p_stake.add_argument("token_id", type=int)
    p_stake.add_argument("pool_address")
    p_stake.add_argument("--confirm", action="store_true",
                         help="REQUIRED to send. Without it, runs dry-preview.")
    p_stake.set_defaults(func=cmd_stake_position)

    p_unstake = sub.add_parser("unstake-position",
                               help="unstake LP NFT, claims AERO first (no time lock)")
    p_unstake.add_argument("token_id", type=int)
    p_unstake.add_argument("pool_address")
    p_unstake.add_argument("--confirm", action="store_true")
    p_unstake.add_argument("--no-claim", action="store_true",
                           help="skip getReward(); withdraw without claiming AERO")
    p_unstake.set_defaults(func=cmd_unstake_position)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
