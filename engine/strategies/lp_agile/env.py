"""engine/strategies/lp_agile/env.py — load .env.lp + validated config.

The .env.lp file holds the DEDICATED LP wallet private key. This module:
  - reads .env.lp into os.environ (only fills if not already set)
  - exposes a typed config dict
  - validates that LP_WALLET_PRIVATE_KEY derives to LP_WALLET_ADDRESS
  - **NEVER echoes or returns the private key**

Per [[feedback-lp-dedicated-wallet]] the key never lives outside this
process — it's loaded into os.environ inside the Python interpreter
and used only to instantiate an `eth_account.Account`.
"""
from __future__ import annotations

import logging
import os
from decimal import Decimal
from pathlib import Path
from typing import Optional

logger = logging.getLogger("engine.strategies.lp_agile.env")

# Path is fixed by convention. Override with LP_ENV_PATH env var for tests.
LP_ENV_PATH = Path(
    os.environ.get("LP_ENV_PATH")
    or str(Path.home() / "baba" / "wealth-ecosystem" / ".env.lp")
)


def load_lp_env(*, path: Optional[Path] = None, override: bool = False) -> dict:
    """Read .env.lp and inject into os.environ.

    Returns a dict of {var_name: "[set]" | "[empty]"} — never the actual values.

    `override=False` (default) means env vars already set in the shell win over
    .env.lp values. Use `override=True` for tests that need .env.lp to win.
    """
    p = path or LP_ENV_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"{p} not found. Run the .env.lp setup walkthrough from Day 5."
        )
    loaded: dict[str, str] = {}
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if not v:
            loaded[k] = "[empty]"
            continue
        if override or k not in os.environ:
            os.environ[k] = v
        loaded[k] = "[set]"
    return loaded


def get_lp_config() -> dict:
    """Returns the validated LP config dict. NEVER includes the private key."""
    load_lp_env()
    cfg = {
        "wallet_address": _required("LP_WALLET_ADDRESS"),
        "auto_execute":   os.environ.get("LP_AUTO_EXECUTE", "false").lower() == "true",
        "per_position_max_usd":  _decimal("LP_PER_POSITION_MAX_USD", "10"),
        "total_bankroll_usd":    _decimal("LP_TOTAL_BANKROLL_USD",   "30"),
        "gas_fund_floor_usd":    _decimal("LP_GAS_FUND_FLOOR_USD",   "2"),
        "start_chain":  os.environ.get("LP_START_CHAIN",  "base"),
        "start_pool":   os.environ.get("LP_START_POOL",   "aero_slipstream_cbbtc_usdc"),
        "tg_bot_token": os.environ.get("LP_TG_BOT_TOKEN", ""),
        "tg_chat_id":   os.environ.get("LP_TG_CHAT_ID",   ""),
        "target_apr_pct": _decimal("LP_TARGET_APR_PCT", "100"),
    }
    cfg["pk_validated"] = _validate_pk(cfg["wallet_address"])
    cfg["allocation_policy"] = get_allocation_policy(cfg["total_bankroll_usd"])
    return cfg


# 2026-05-29 (#111 Yomi confirmed): tiered multi-platform allocation policy.
# Smart contract risk is non-diversifiable within-platform but linear across.
# Each tier defines per-platform capital caps as fractions of total bankroll.
# The lp_agile executor checks `can_top_up(platform, current_per_platform_usd)`
# before any add-liquidity / open-position tx and refuses if it would breach.
ALLOCATION_POLICY = {
    "validation":  {  # $0 – $5K
        "tier_max_usd":      Decimal("5000"),
        "platform_caps": {
            "aerodrome":  Decimal("0.40"),   # 40%
            "prjx":       Decimal("0.60"),   # 60%
        },
        "hardware_wallet_required": False,
        "name": "validation",
    },
    "test":  {  # $5K – $25K
        "tier_max_usd":      Decimal("25000"),
        "platform_caps": {
            "aerodrome":  Decimal("0.33"),
            "prjx":       Decimal("0.34"),
            "uniswap_v3": Decimal("0.33"),
        },
        "hardware_wallet_required": False,
        "name": "test",
    },
    "commercial":  {  # $25K+
        "tier_max_usd":      None,            # no cap
        "platform_caps": {
            "aerodrome":  Decimal("0.33"),
            "prjx":       Decimal("0.34"),
            "uniswap_v3": Decimal("0.33"),
        },
        "hardware_wallet_required": True,     # Ledger/Trezor non-negotiable
        "name": "commercial",
    },
}


def get_allocation_policy(total_bankroll_usd) -> dict:
    """Return the active allocation tier given current total bankroll. Tier
    flips automatically as bankroll grows."""
    b = Decimal(str(total_bankroll_usd or 0))
    if b < Decimal("5000"):
        return ALLOCATION_POLICY["validation"]
    if b < Decimal("25000"):
        return ALLOCATION_POLICY["test"]
    return ALLOCATION_POLICY["commercial"]


def can_top_up(platform: str, current_platform_usd, top_up_usd,
               total_bankroll_usd) -> tuple[bool, str]:
    """Pre-flight check: would adding `top_up_usd` to `platform` breach the
    current tier's cap? Returns (allowed, reason).

    Examples:
      can_top_up("prjx", 100, 50, 200)
        bankroll 200 → validation tier → prjx cap 60% × 200 = $120 → 100+50=150 > 120 → False
      can_top_up("aerodrome", 40, 20, 200)
        bankroll 200 → aero cap 40% × 200 = $80 → 40+20=60 ≤ 80 → True
    """
    p = (platform or "").lower()
    cur = Decimal(str(current_platform_usd or 0))
    add = Decimal(str(top_up_usd or 0))
    pol = get_allocation_policy(total_bankroll_usd)
    if p not in pol["platform_caps"]:
        return False, (
            f"platform {p!r} not authorised in tier {pol['name']}. "
            f"Allowed: {sorted(pol['platform_caps'].keys())}. "
            f"Upgrade tier (more bankroll) to unlock."
        )
    cap_pct = pol["platform_caps"][p]
    cap_usd = cap_pct * Decimal(str(total_bankroll_usd or 0))
    if cur + add > cap_usd:
        return False, (
            f"{p} cap breached: tier={pol['name']} cap={cap_pct*100}% × "
            f"${total_bankroll_usd} = ${cap_usd:.0f} ; current=${cur:.0f} ; "
            f"top_up=${add:.0f} ; total_after=${cur+add:.0f}"
        )
    # Hardware-wallet gate for commercial tier
    if pol["hardware_wallet_required"]:
        if os.environ.get("LP_HARDWARE_WALLET_ATTESTED", "false").lower() != "true":
            return False, (
                f"tier={pol['name']} REQUIRES hardware wallet attestation. "
                f"Set LP_HARDWARE_WALLET_ATTESTED=true in .env.lp after migrating "
                f"keys to Ledger/Trezor. ${cap_usd:.0f}+ capital at risk."
            )
    return True, (
        f"OK: {p} after top-up will be ${cur+add:.0f} ≤ cap ${cap_usd:.0f} "
        f"(tier={pol['name']})"
    )


def get_signer():
    """Returns an eth_account.Account derived from LP_WALLET_PRIVATE_KEY.

    Caller-side rule: NEVER log, print, repr, or pickle the returned object.
    The .key attribute on Account is the raw private key — keep it in memory only.
    """
    load_lp_env()
    pk = os.environ.get("LP_WALLET_PRIVATE_KEY")
    if not pk:
        raise ValueError(
            "LP_WALLET_PRIVATE_KEY not set in .env.lp — "
            "auto-execute is impossible. Fill in before retrying."
        )
    from eth_account import Account
    try:
        return Account.from_key(pk)
    except Exception as e:                                # noqa: BLE001
        # Don't surface the exception detail — could leak fragments of the key.
        raise ValueError(
            f"LP_WALLET_PRIVATE_KEY in .env.lp is malformed "
            f"(eth_account rejected: {type(e).__name__}). "
            f"Should be 64 hex chars, optionally with 0x prefix, no quotes."
        ) from None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _required(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise ValueError(f".env.lp missing required {name}")
    return v


def _decimal(name: str, default: str) -> Decimal:
    try:
        return Decimal(os.environ.get(name) or default)
    except Exception:                                     # noqa: BLE001
        logger.warning("%s malformed, falling back to %s", name, default)
        return Decimal(default)


def _validate_pk(expected_address: str) -> bool:
    """Cross-check that the private key derives to the stated wallet address.

    Returns True on match. Raises on mismatch (loud — this is a config bug
    that would otherwise sign txs to the wrong address).
    Returns False (no exception) when private key not set — execution simply
    not available, but config still loads for read-only tooling.
    """
    pk = os.environ.get("LP_WALLET_PRIVATE_KEY")
    if not pk:
        return False
    from eth_account import Account
    try:
        derived = Account.from_key(pk).address
    except Exception:                                     # noqa: BLE001
        raise ValueError(
            "LP_WALLET_PRIVATE_KEY malformed — cannot derive address"
        ) from None
    if derived.lower() != expected_address.lower():
        raise ValueError(
            f"WALLET MISMATCH: private key derives to {derived} but "
            f"LP_WALLET_ADDRESS in .env.lp says {expected_address}. "
            f"Auto-execute REFUSED — fix .env.lp before retrying."
        )
    return True
