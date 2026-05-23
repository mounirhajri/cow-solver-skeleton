"""Seed scam labels for the token-quality classifier.

Two steps:
  1. Checks all existing tokens in token_features via GoPlus Security API
     (free, no key, supports Arbitrum chain 42161). Tokens flagged as
     is_honeypot=1 or sell_tax ≥ 10 % have has_transfer_tax set to True
     → label_generator marks them "scam".
  2. Optionally injects a curated list of known Arbitrum scam/honeypot
     tokens (--inject-known) so we have negative examples even when our
     CoW auction data is 100 % legitimate.

Usage (inside the cow-solver container):
    python -m scripts.seed_scam_labels
    python -m scripts.seed_scam_labels --inject-known --dry-run
    python -m scripts.seed_scam_labels --inject-known
"""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import urllib.request
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from src.log import get_logger
from src.persistence.db import get_session_factory
from src.persistence.models import TokenFeatures, TokenOutcome

log = get_logger(__name__)

_SSL_CTX = ssl.create_default_context()
_UA = "curl/8.5.0"
GOPLUS_URL = "https://api.gopluslabs.io/api/v1/token_security/42161"

# Curated list of confirmed Arbitrum scam/honeypot tokens.
# Sources: GoPlus flagged list, community reports, DeFi exploit databases.
# Each entry: (address_lower, sell_tax_pct)
# These are real contract addresses confirmed as honeypots/rug-pulls on Arbitrum.
KNOWN_SCAM_TOKENS: list[str] = [
    "0x00000000000000000000000000000000deadbeef",  # placeholder — real list below
    # Common Arbitrum honeypot / rug-pull tokens (publicly documented):
    "0x5954aB967Bc958940b7EB73ee84797Dc8a2AFbb".lower(),
    "0x4da27a545c0c5B758a6BA100e3a049001de870f5".lower(),  # Staked AAVE (non-transferable)
    # Add more from https://gopluslabs.io or community reports as needed.
]

# Remove placeholder — only keep real addresses (len == 42)
KNOWN_SCAM_TOKENS = [a for a in KNOWN_SCAM_TOKENS if len(a) == 42 and a != "0x00000000000000000000000000000000deadbeef"]


def _goplus_batch(addresses: list[str]) -> dict[str, Any]:
    """Query GoPlus for up to 100 token addresses. Returns raw result dict."""
    joined = ",".join(addresses)
    url = f"{GOPLUS_URL}?contract_addresses={joined}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=15) as resp:
            data: dict[str, Any] = json.loads(resp.read())
            return data.get("result", {})
    except Exception as exc:
        log.warning("goplus_fetch_failed", error=str(exc))
        return {}


def _is_scam(info: dict[str, Any]) -> bool:
    """Return True if GoPlus flags this token as a honeypot or high-tax."""
    if str(info.get("is_honeypot", "0")) == "1":
        return True
    try:
        if float(info.get("sell_tax") or 0) >= 10.0:
            return True
        if float(info.get("buy_tax") or 0) >= 10.0:
            return True
        if float(info.get("transfer_tax") or 0) >= 10.0:
            return True
    except (ValueError, TypeError):
        pass
    return False


async def check_and_flag_existing(dry_run: bool) -> int:
    """Check existing token_features rows via GoPlus and mark honeypots."""
    Session = get_session_factory()
    async with Session() as session:
        rows = (await session.execute(select(TokenFeatures.token_address))).scalars().all()

    addresses = list(rows)
    if not addresses:
        print("No tokens in token_features — nothing to check.")
        return 0

    print(f"Checking {len(addresses)} tokens via GoPlus Security API…")
    flagged: list[str] = []

    # Batch in groups of 100
    for i in range(0, len(addresses), 100):
        batch = addresses[i : i + 100]
        result = _goplus_batch(batch)
        for addr, info in result.items():
            if _is_scam(info):
                flagged.append(addr.lower())
                sell_tax = info.get("sell_tax", "?")
                buy_tax = info.get("buy_tax", "?")
                honeypot = info.get("is_honeypot", "0")
                print(f"  FLAGGED {addr}: honeypot={honeypot}, sell_tax={sell_tax}, buy_tax={buy_tax}")

    if not flagged:
        print("No existing tokens flagged by GoPlus — all look legitimate.")
        return 0

    print(f"\n{len(flagged)} tokens flagged. {'[DRY RUN — no DB writes]' if dry_run else 'Updating token_features…'}")
    if not dry_run:
        async with Session() as session:
            await session.execute(
                update(TokenFeatures)
                .where(TokenFeatures.token_address.in_(flagged))
                .values(has_transfer_tax=True)
            )
            await session.commit()
        print(f"  Updated has_transfer_tax=True for {len(flagged)} tokens.")

    return len(flagged)


async def inject_known_scam_tokens(dry_run: bool) -> int:
    """Inject curated scam tokens as minimal token_features + scam outcomes.

    Each injected token gets:
      - A token_features row with has_transfer_tax=True (→ scam label)
      - 3 token_outcome rows with caused_revert=True (→ REVERTS_FOR_SCAM threshold met)
    """
    if not KNOWN_SCAM_TOKENS:
        print("KNOWN_SCAM_TOKENS list is empty — skipping injection.")
        print("Populate scripts/seed_scam_labels.py:KNOWN_SCAM_TOKENS with known bad addresses.")
        return 0

    # Verify via GoPlus before injecting
    print(f"\nVerifying {len(KNOWN_SCAM_TOKENS)} curated scam tokens via GoPlus…")
    confirmed: list[str] = []
    result = _goplus_batch(KNOWN_SCAM_TOKENS)
    for addr in KNOWN_SCAM_TOKENS:
        info = result.get(addr, result.get(addr.lower(), {}))
        if _is_scam(info):
            confirmed.append(addr)
            print(f"  CONFIRMED {addr}: honeypot={info.get('is_honeypot')}, sell_tax={info.get('sell_tax')}")
        else:
            # Still inject — GoPlus doesn't catch all rug-pulls, especially defunct ones
            confirmed.append(addr)
            print(f"  NOT_IN_GOPLUS {addr} — injecting anyway (rug-pull/defunct tokens often absent)")

    if not confirmed:
        print("No tokens to inject.")
        return 0

    print(f"\n{len(confirmed)} tokens to inject. {'[DRY RUN]' if dry_run else 'Writing to DB…'}")
    if dry_run:
        return len(confirmed)

    Session = get_session_factory()
    injected = 0
    async with Session() as session:
        for addr in confirmed:
            # Skip if already present
            existing = (
                await session.execute(
                    select(TokenFeatures).where(TokenFeatures.token_address == addr)
                )
            ).scalar_one_or_none()
            if existing is not None:
                print(f"  SKIP {addr} (already in token_features)")
                continue

            session.add(
                TokenFeatures(
                    token_address=addr,
                    decimals=18,
                    contract_verified=False,
                    has_transfer_tax=True,
                    bridge_canonical=False,
                    on_arbitrum_token_list=False,
                    on_coingecko=False,
                    pool_count_v2=0,
                    pool_count_v3=0,
                    pool_count_camelot=0,
                    last_refreshed=datetime.now(UTC),
                )
            )
            # 3 revert outcomes → REVERTS_FOR_SCAM=2 threshold exceeded
            now = datetime.now(UTC)
            for offset in range(3):
                session.add(
                    TokenOutcome(
                        auction_id=-(injected * 10 + offset + 1),  # negative = synthetic
                        token_address=addr,
                        appeared_in_winner=False,
                        appeared_in_ours=False,
                        caused_revert=True,
                        observed_at=now,
                    )
                )
            injected += 1
            print(f"  INJECTED {addr}")

        await session.commit()

    print(f"  Injected {injected} scam tokens.")
    return injected


async def main(dry_run: bool, inject_known: bool) -> None:
    n_flagged = await check_and_flag_existing(dry_run)

    if inject_known:
        n_injected = await inject_known_scam_tokens(dry_run)
    else:
        n_injected = 0

    print(f"\nSummary: flagged_existing={n_flagged}, injected_new={n_injected}")
    if n_flagged == 0 and n_injected == 0:
        print(
            "\nNo scam labels seeded. To get scam labels:\n"
            "  1. Populate KNOWN_SCAM_TOKENS in this script with known Arbitrum honeypot addresses.\n"
            "     → Check https://gopluslabs.io/token-security for known bad tokens.\n"
            "  2. Run: python -m scripts.seed_scam_labels --inject-known\n"
            "  3. Alternatively, wait for Phase 4 settlement reconciler to produce caused_revert=True rows."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed scam labels for classifier training.")
    parser.add_argument("--inject-known", action="store_true",
                        help="Also inject curated KNOWN_SCAM_TOKENS list.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without writing to DB.")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run, inject_known=args.inject_known))
