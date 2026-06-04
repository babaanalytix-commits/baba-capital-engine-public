"""engine/strategies/lp_agile/ai_judge.py — LP-tailored AI judge.

Reuses the same tiered infrastructure as the existing ORACLE judge
(`ops/bmi/baba_ai_judge`) but with LP-specific prompts and inputs. Three
tiers, same cost economics:

  Tier 1 — rule pre-filter (free, deterministic)
           No news on base asset OR pool protocol → PASS.
           News present → escalate to Tier 2.

  Tier 2 — Gemini 2.0 Flash (free, 15 req/min)
           Reads pool state + news + macro context, returns PASS/WATCH/BLOCKED.
           Always engages when GEMINI_API_KEY present (per Yomi 2026-05-19).

  Tier 3 — Haiku/Claude shadow (paid, capped $5/day)
           A/B logged for 30d before being promoted to live decisions.
           Gated by CONTEXT_LAYER_SPENDING_AUTHORIZED env flag.

Output: JudgeOutcome (verdict, tier_used, annotation, source_url, confidence)
attached to every LPSignal before the alerter renders subscriber-facing text.

Failure mode: judge call failed / no API key → verdict = "PASS" with annotation
"AI judge unavailable — rule-only PASS". Trustless logging marks the source as
"rule_fallback" so subscribers know it's not an LLM verdict.

Related memories: [[feedback-trustless-data-verification]] (every read tagged
source + age), [[feedback-ask-before-spending]] (Tier 3 paid is gated).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from engine.strategies.lp_agile.types import LPAction, LPSignal

logger = logging.getLogger("engine.strategies.lp_agile.ai_judge")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
FREE_NEWS_DB = _REPO_ROOT / "domains" / "fundamentals" / "data" / "fundamentals.db"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# 2026-05-31: gemini-2.0-flash now returns 404 from Googles API. Switch to
# 2.5-flash (the model MD AI triage already uses successfully). Make it env-
# tunable for future swaps without touching code, matching the pattern in
# engine/strategies/md_ai/triage.py:46 and oracle_ai/triage.py:48.
GEMINI_MODEL = os.environ.get("LP_GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={{k}}"
)

PROTOCOL_NEWS_KEYWORDS = {
    "prjx":        ["prjx", "project x", "hyperswap", "hyperevm"],
    "uniswap_v3":  ["uniswap"],
    "slipstream":  ["aerodrome", "slipstream"],
    "aerodrome":   ["aerodrome"],
}


# ---------------------------------------------------------------------------
# Verdict type (matches engine.ai.signal_judge.JudgeOutcome shape)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LPJudgeOutcome:
    verdict: str               # "PASS" | "WATCH" | "BLOCKED" | "UNAVAILABLE"
    tier_used: str             # "tier1_rule" | "tier2_gemini" | "tier3_haiku" | "rule_fallback"
    annotation: str = ""       # ≤30 words for WATCH; reason for BLOCKED
    source_url: str = ""
    confidence: float = 0.0
    cost_usd: float = 0.0
    news_items_considered: int = 0
    timestamp_iso: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def judge_lp_signal(signal: LPSignal) -> LPJudgeOutcome:
    """Judge one LP signal. NEVER raises — returns UNAVAILABLE or PASS on error.

    The signal's `ai_judge_*` fields can be replaced from this outcome via:
        from dataclasses import replace
        signal = replace(signal,
            ai_judge_verdict=outcome.verdict,
            ai_judge_tier=outcome.tier_used,
            ai_judge_reasoning=outcome.annotation,
        )
    """
    pool = signal.pool

    # Gather news context for both the base asset AND the protocol
    asset_news = _query_news_for_keyword(pool.base_symbol, hours=12, limit=5)
    proto_keywords = PROTOCOL_NEWS_KEYWORDS.get(pool.protocol.value, [])
    proto_news: list[dict] = []
    for kw in proto_keywords:
        proto_news.extend(_query_news_for_keyword(kw, hours=24, limit=3))
    # Dedup by URL
    seen_urls = set()
    proto_news_dedup = []
    for n in proto_news:
        url = n.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            proto_news_dedup.append(n)

    all_news = asset_news + proto_news_dedup

    # Tier 1: no news at all + no Gemini key → cheap PASS
    if not all_news and not GEMINI_API_KEY:
        return LPJudgeOutcome(
            verdict="PASS",
            tier_used="tier1_rule",
            annotation="No recent news on base asset or protocol — rule-PASS",
            confidence=1.0,
            news_items_considered=0,
        )

    # Tier 2: Gemini with LP-specific prompt
    if GEMINI_API_KEY:
        outcome = _tier2_gemini_judge(signal, asset_news, proto_news_dedup)
        if outcome is not None:
            return outcome

    # Tier 2 unavailable + we had news → conservative WATCH so subscribers know
    if all_news:
        first = all_news[0]
        return LPJudgeOutcome(
            verdict="WATCH",
            tier_used="rule_fallback",
            annotation=(f"News exists for {pool.base_symbol} or "
                        f"{pool.protocol.value} but AI judge unavailable — review manually"),
            source_url=first.get("url", ""),
            confidence=0.4,
            news_items_considered=len(all_news),
        )

    # Last resort
    return LPJudgeOutcome(
        verdict="PASS",
        tier_used="rule_fallback",
        annotation="No news, no AI — rule-PASS",
        confidence=0.6,
        news_items_considered=0,
    )


# ---------------------------------------------------------------------------
# Tier 1 helper: news lookup (free, reads fundamentals.db)
# ---------------------------------------------------------------------------


def _query_news_for_keyword(keyword: str, *, hours: int, limit: int) -> list[dict]:
    """Return news items mentioning `keyword` in last N hours."""
    if not FREE_NEWS_DB.exists():
        return []
    try:
        con = sqlite3.connect(str(FREE_NEWS_DB))
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cutoff_ts = int(time.time()) - (hours * 3600)
        for table in ("news_items", "free_news", "news"):
            try:
                cur.execute(
                    f"""SELECT title, url, source, published_at
                        FROM {table}
                        WHERE (title LIKE ? OR title LIKE ?)
                          AND published_at >= ?
                        ORDER BY published_at DESC LIMIT ?""",
                    (f"%{keyword.upper()}%", f"%{keyword.lower()}%", cutoff_ts, limit),
                )
                rows = [dict(r) for r in cur.fetchall()]
                if rows:
                    con.close()
                    return rows
            except sqlite3.OperationalError:
                continue
        con.close()
    except Exception as e:                                # noqa: BLE001
        logger.info("news query failed for %s: %s", keyword, e)
    return []


# ---------------------------------------------------------------------------
# Tier 2: Gemini with LP-specific prompt
# ---------------------------------------------------------------------------


LP_GEMINI_SYSTEM_PROMPT = """You are BABA AI, a strict second-opinion layer for concentrated-liquidity LP recommendations.

You receive:
  - An LP signal: ACTION (OPEN/CLOSE/REBALANCE/HOLD), POOL (protocol, pair, fee tier)
  - Pool snapshot: TVL, current price, 24h volume, estimated fee APR
  - Recent news on the base asset (last 12h) — may be empty
  - Recent news on the protocol (last 24h) — may be empty

Your job: decide ONE verdict for this LP action:
  PASS — recommendation is sound, no specific concerns. Default.
  WATCH — recommendation is OK but trader should know a specific concern (e.g.
          "AERO emissions epoch ending — APR may halve next week", or
          "PRJX governance vote on fee switch tomorrow", or
          "cbBTC volume concentrated in 1 trader, sustainability uncertain")
  BLOCKED — recommendation is dangerous given news/state:
          - Pool contract exploit or audit revocation reported
          - Protocol governance attack or critical bug
          - Base asset depeg / rugpull signal
          - Sudden TVL collapse (>50%) suggesting incident

Rules:
  1. Default to PASS. WATCH only for SPECIFIC concrete concerns.
  2. BLOCK only when news directly implicates the pool, protocol, or base asset
     with material loss-of-funds risk.
  3. Annotation MUST be ≤30 words. Be punchy. Cite specific data points or news.
  4. Source URL ONLY from provided news items — never invent.
  5. Output STRICT JSON: {"verdict": "PASS|WATCH|BLOCKED", "annotation": "...", "source_url": "...", "confidence": 0.0-1.0}
"""


def _tier2_gemini_judge(
    signal: LPSignal, asset_news: list[dict], proto_news: list[dict],
) -> Optional[LPJudgeOutcome]:
    pool = signal.pool
    snap = signal.snapshot_at_signal

    user_lines = [
        f"Signal: {signal.action.value.upper()} {pool.pair} on "
        f"{pool.protocol.value} (chain={pool.chain.value}, fee={pool.fee_tier_bps}bps)",
    ]
    if snap is not None:
        user_lines.extend([
            f"Pool TVL: ${float(snap.tvl_usd):,.0f}",
            f"24h volume: ${float(snap.volume_24h_usd):,.0f}",
            f"Fee APR: {float(snap.fee_apr)*100:.1f}%",
            f"Current price: ${float(snap.base_price_usd):,.6f}",
        ])
    user_lines.append(f"Range: ${float(signal.range_low_price):,.4f} → "
                      f"${float(signal.range_high_price):,.4f}  ({signal.range_label})")
    user_lines.append("")
    user_lines.append(f"Recent news on {pool.base_symbol} ({len(asset_news)} items):")
    if not asset_news:
        user_lines.append("  (none)")
    for n in asset_news[:5]:
        user_lines.append(f"  - [{n.get('source', '?')}] {n.get('title', '')}")
        if n.get('url'):
            user_lines.append(f"    {n['url']}")
    user_lines.append("")
    user_lines.append(f"Recent news on {pool.protocol.value} ({len(proto_news)} items):")
    if not proto_news:
        user_lines.append("  (none)")
    for n in proto_news[:3]:
        user_lines.append(f"  - [{n.get('source', '?')}] {n.get('title', '')}")
        if n.get('url'):
            user_lines.append(f"    {n['url']}")

    user_prompt = "\n".join(user_lines)

    payload = {
        "system_instruction": {"parts": [{"text": LP_GEMINI_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }

    try:
        req = urllib.request.Request(
            GEMINI_URL.format(k=GEMINI_API_KEY),
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode())
        text = raw["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        return LPJudgeOutcome(
            verdict=str(parsed.get("verdict", "PASS")).upper(),
            tier_used="tier2_gemini",
            annotation=str(parsed.get("annotation", ""))[:200],
            source_url=str(parsed.get("source_url", "")),
            confidence=float(parsed.get("confidence", 0.5)),
            cost_usd=0.0,
            news_items_considered=len(asset_news) + len(proto_news),
        )
    except Exception as e:                                # noqa: BLE001
        logger.warning("gemini LP judge failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Convenience: judge + attach verdict to signal
# ---------------------------------------------------------------------------


def judge_and_annotate(signal: LPSignal) -> LPSignal:
    """Run the judge and return a NEW LPSignal with verdict fields populated."""
    from dataclasses import replace
    outcome = judge_lp_signal(signal)
    return replace(
        signal,
        ai_judge_verdict=outcome.verdict,
        ai_judge_tier=outcome.tier_used,
        ai_judge_reasoning=(outcome.annotation
                            + (f"  [src: {outcome.source_url}]"
                               if outcome.source_url else "")),
    )
