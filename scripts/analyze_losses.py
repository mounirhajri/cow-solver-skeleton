"""Loss decomposition: what do the winners have that we don't?

One-command weekly tool replicating the 2026-06-12 hand analysis that surfaced
Uniswap V4 as our biggest missing lever. For every auction where our feasible
router-v2 score lost to the winner by a real margin, it decomposes WHAT the
winner actually executed:

  1. DB        — real-margin losses: router-v2 rows with feasible=True and
                 our_score_wei < --ratio × the winner's own score
                 (shadow_competitors.is_winner, score > 0), window --hours.
  2. CoW API   — live v2 ``solver_competition/{auction_id}`` per lost auction →
                 classify the winner's solution shape (cow-match / single-order
                 / multi-order-batch) and collect the settlement txHash.
  3. On-chain  — fetch each settlement receipt and classify every event log
                 against the venue table below (Uniswap V3 / V2-style / V4,
                 Balancer V2 / V3, Curve). Uniswap V4 is matched primarily by
                 the PoolManager singleton ADDRESS — we identified the contract
                 via Blockscout — because hookable pools may vary the event
                 while the singleton address cannot.
  4. Print     — loss-class table, hops histogram, venue counter, worst-10
                 losses, unknown-venue table, and a one-line SUMMARY verdict
                 ("dominant winner edge: X").

Usage
-----
    python -m scripts.analyze_losses [--hours 24] [--limit 40] [--ratio 0.99] \\
        [--skip-receipts]

``--skip-receipts`` is the quick API-only mode: loss classes and worst-10 only,
no RPC receipts (so no venue/hops data).

Run inside the cow-solver container so it reads the prod DB + RPC config:
    docker compose exec cow-solver python -m scripts.analyze_losses --hours 24
"""

from __future__ import annotations

import argparse
import asyncio
import json
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from eth_typing import HexStr
from sqlalchemy import select
from web3 import Web3

from src.config import settings
from src.log import get_logger
from src.persistence.db import get_session_factory
from src.persistence.models import ShadowCompetitor, ShadowSolution
from src.shadow.cow_api import _SSL_CTX, _UA

log = get_logger(__name__)

# CoW moved solver_competition to /api/v2 in 2026-06 (v1 now 404s).
COW_COMPETITION_URL = (
    "https://api.cow.fi/arbitrum_one/api/v2/solver_competition/{auction_id}"
)
API_THROTTLE_S = 0.2
RECEIPT_THROTTLE_S = 0.15

# ---------------------------------------------------------------------------
# Venue classification tables
# ---------------------------------------------------------------------------

# Uniswap V4 PoolManager singleton on Arbitrum. ALL V4 swaps emit from this one
# address (hooks included), so we classify by ADDRESS, not topic — we identified
# the contract via Blockscout after observing topic 0x40e9cecb… live.
UNISWAP_V4_POOL_MANAGER = "0x360e68faccca8ca495c1b759fd9eee466db9fb32"
# Balancer V3 Vault singleton (same pattern: one vault, classify by address).
BALANCER_V3_VAULT = "0xba1333333333a1ba1108e8412f11850a5c319ba9"

VENUE_BY_ADDRESS: dict[str, str] = {
    UNISWAP_V4_POOL_MANAGER: "uniswap-v4",
    BALANCER_V3_VAULT: "balancer-v3",
}

# topic0 → venue label, for venues where the pool contract address varies.
VENUE_TOPICS: dict[str, str] = {
    # Uniswap V3 Pool: Swap(sender, recipient, amount0, amount1, sqrtPriceX96, …)
    "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67": "uniswap-v3",
    # UniswapV2-style Pair: Swap(sender, amount0In, amount1In, …) — Sushi/Camelot
    # V2 forks share the exact signature, so this is "v2-style", not one DEX.
    "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822": "v2-style",
    # Balancer V2 Vault: Swap(poolId, tokenIn, tokenOut, amountIn, amountOut)
    "0x2170c741c41531aec20e7c107c24eeecdd15e0a1ee4d211c8902a82e44193371": "balancer-v2",
    # Curve: TokenExchange(buyer, sold_id, tokens_sold, bought_id, tokens_bought)
    "0x8b3e96f2b889fa771c53c981b40daf005f63f637f1869f707052d15a3dd97140": "curve-exchange",
    # Uniswap V4 PoolManager Swap — observed live 2026-06-12. Kept as a topic
    # fallback only; the PRIMARY V4 match is the PoolManager address above.
    "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f": "uniswap-v4",
}

# Token movement / plumbing — explicitly NOT venues. (Transfer and Approval have
# 3 topics and would otherwise pollute the unknown-venue bucket.)
IGNORED_TOPICS: frozenset[str] = frozenset(
    {
        # ERC-20 Transfer(from, to, value)
        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
        # ERC-20 Approval(owner, spender, value)
        "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925",
        # WETH Deposit(dst, wad) / Withdrawal(src, wad)
        "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c",
        "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65",
    }
)

# An unknown event only counts as a candidate venue when it carries this many
# topics — sparse events (≤2 topics: GPv2 Trade/Settlement, oracle pokes, …)
# are settlement plumbing, not swap legs.
UNKNOWN_MIN_TOPICS = 3

_UNKNOWN = "unknown-venue"

# Loss-class labels (winner solution shape).
CLASS_COW_MATCH = "cow-match"
CLASS_SINGLE = "single-order"
CLASS_MULTI = "multi-order-batch"
CLASS_EMPTY = "empty"


# ---------------------------------------------------------------------------
# Pure classifiers (unit-tested, no I/O)
# ---------------------------------------------------------------------------


def _order_pair(order: dict[str, Any]) -> tuple[str, str] | None:
    """(sell_token, buy_token) of a winner-solution order dict, lower-cased.

    Prefers explicit sellToken/buyToken keys; falls back to decoding the pair
    out of the order UID (0x + 32-byte hash + 20-byte sell + 20-byte buy)."""
    sell = order.get("sellToken")
    buy = order.get("buyToken")
    if isinstance(sell, str) and isinstance(buy, str) and sell and buy:
        return sell.lower(), buy.lower()
    uid = order.get("id") or order.get("uid") or ""
    if isinstance(uid, str) and len(uid) == 146 and uid.startswith("0x"):
        return "0x" + uid[66:106].lower(), "0x" + uid[106:146].lower()
    return None


def classify_solution(orders: list[Any]) -> str:
    """Classify a winner solution's shape from its ``orders`` array.

    cow-match = exactly two orders trading the SAME pair in OPPOSITE directions
    (the winner crossed them peer-to-peer — no AMM needed)."""
    n = len(orders)
    if n == 0:
        return CLASS_EMPTY
    if n == 1:
        return CLASS_SINGLE
    if n == 2:
        pair_a = _order_pair(orders[0]) if isinstance(orders[0], dict) else None
        pair_b = _order_pair(orders[1]) if isinstance(orders[1], dict) else None
        if pair_a is not None and pair_b is not None and pair_a == (pair_b[1], pair_b[0]):
            return CLASS_COW_MATCH
    return CLASS_MULTI


def pick_winner_solution(comp: dict[str, Any]) -> dict[str, Any] | None:
    """First solution flagged isWinner (or ranking==1) in a v2 competition body."""
    solutions = comp.get("solutions") or []
    if not isinstance(solutions, list):
        return None
    for sol in solutions:
        if isinstance(sol, dict) and (sol.get("isWinner") or sol.get("ranking") == 1):
            return sol
    return None


def winner_tx_hash(comp: dict[str, Any], winner: dict[str, Any]) -> str | None:
    """Settlement txHash for the winner solution, lower-cased, or None."""
    tx = winner.get("txHash")
    if isinstance(tx, str) and tx.startswith("0x"):
        return tx.lower()
    # Some response variants carry the hash(es) at the top level instead.
    hashes = comp.get("transactionHashes")
    if isinstance(hashes, list) and hashes and isinstance(hashes[0], str):
        return hashes[0].lower()
    return None


def classify_log(address: str, topics: list[str]) -> str | None:
    """Venue label for one receipt log, ``unknown-venue`` candidate, or None.

    Order matters: the V4 PoolManager / Balancer-V3 Vault singletons match by
    ADDRESS first (any event they emit is venue activity — topics may vary with
    hooks), then known topics, then the ignore list, then the unknown bucket."""
    addr = address.lower()
    by_addr = VENUE_BY_ADDRESS.get(addr)
    if by_addr is not None:
        return by_addr
    if not topics:
        return None
    topic0 = topics[0].lower()
    venue = VENUE_TOPICS.get(topic0)
    if venue is not None:
        return venue
    if topic0 in IGNORED_TOPICS:
        return None
    if len(topics) >= UNKNOWN_MIN_TOPICS:
        return _UNKNOWN
    return None


def classify_receipt_logs(
    logs: Iterable[tuple[str, list[str]]],
) -> tuple[Counter[str], Counter[str]]:
    """(venue_counts, unknown_counts) over (address, topics) log views.

    Unknown keys carry the address + topic0 prefix so a recurring unknown venue
    is identifiable (paste the address into Blockscout, like we did for V4)."""
    venues: Counter[str] = Counter()
    unknown: Counter[str] = Counter()
    for address, topics in logs:
        label = classify_log(address, topics)
        if label == _UNKNOWN:
            topic0 = topics[0].lower() if topics else "(none)"
            unknown[f"{address.lower()} {topic0[:10]}…"] += 1
        elif label is not None:
            venues[label] += 1
    return venues, unknown


@dataclass
class Loss:
    """One auction we lost by real margin, progressively enriched."""

    auction_id: int
    our_score: int
    winner_score: int
    loss_class: str = "(api-error)"
    n_orders: int = 0
    tx_hash: str | None = None
    n_swaps: int = -1  # -1 = no receipt data
    venues: Counter[str] = field(default_factory=Counter)

    @property
    def ratio(self) -> float:
        return self.our_score / self.winner_score if self.winner_score else float("nan")


def compute_losses(
    rows: Iterable[tuple[int, Any, Any]],
    ratio_threshold: float,
    limit: int,
) -> list[Loss]:
    """Pure filter: (auction_id, our_score_wei, winner_score) rows → worst losses.

    Dedupes per auction keeping our BEST score (so the loss margin is honest),
    drops non-positive/missing winner scores, keeps ratio < threshold, sorts
    worst-first (lowest ratio) and caps at ``limit``."""
    best: dict[int, tuple[int, int]] = {}
    for auction_id, our_raw, winner_raw in rows:
        if our_raw is None or winner_raw is None:
            continue
        our, winner = int(our_raw), int(winner_raw)
        if winner <= 0:
            continue
        current = best.get(auction_id)
        if current is None or our > current[0]:
            best[auction_id] = (our, winner)
    losses = [
        Loss(auction_id=aid, our_score=our, winner_score=winner)
        for aid, (our, winner) in best.items()
        if our < ratio_threshold * winner
    ]
    losses.sort(key=lambda loss: loss.ratio)
    return losses[:limit]


# ---------------------------------------------------------------------------
# I/O shells (thin, NOT unit-tested: DB query, CoW API, receipts)
# ---------------------------------------------------------------------------


async def fetch_loss_rows(hours: int) -> list[tuple[int, Any, Any]]:
    """DB: (auction_id, our_score_wei, winner_score) for feasible router-v2 rows."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    factory = get_session_factory()
    async with factory() as session:
        q = (
            select(
                ShadowSolution.auction_id,
                ShadowSolution.our_score_wei,
                ShadowCompetitor.score,
            )
            .join(
                ShadowCompetitor,
                ShadowCompetitor.auction_id == ShadowSolution.auction_id,
            )
            .where(ShadowSolution.strategy == "router-v2")
            .where(ShadowSolution.feasible.is_(True))
            .where(ShadowSolution.our_score_wei.is_not(None))
            .where(ShadowSolution.created_at >= since)
            .where(ShadowCompetitor.is_winner.is_(True))
            .where(ShadowCompetitor.score.is_not(None))
        )
        rows = (await session.execute(q)).all()
    return [(int(r[0]), r[1], r[2]) for r in rows]


def _http_get_json(url: str) -> dict[str, Any] | None:
    """GET → JSON dict via urllib (CoW API blocks httpx's TLS fingerprint)."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, context=_SSL_CTX, timeout=10) as resp:
        result: dict[str, Any] = json.loads(resp.read())
        return result


async def fetch_competition(auction_id: int) -> dict[str, Any] | None:
    """Live v2 competition body for one auction; None on 404."""
    url = COW_COMPETITION_URL.format(auction_id=auction_id)
    try:
        return await asyncio.to_thread(_http_get_json, url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _receipt_log_views(receipt: Any) -> list[tuple[str, list[str]]]:
    """Normalise a web3 receipt into plain (address, [topic_hex, …]) tuples."""
    views: list[tuple[str, list[str]]] = []
    for entry in receipt.get("logs", []) or []:
        address = str(entry.get("address", "")).lower()
        topics: list[str] = []
        for topic in entry.get("topics", []) or []:
            if isinstance(topic, str):
                topics.append(topic.lower())
            else:  # HexBytes
                topics.append("0x" + bytes(topic).hex())
        views.append((address, topics))
    return views


async def fetch_receipt_logs(w3: Web3, tx_hash: str) -> list[tuple[str, list[str]]]:
    receipt = await asyncio.to_thread(w3.eth.get_transaction_receipt, HexStr(tx_hash))
    return _receipt_log_views(receipt)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _print_loss_classes(losses: list[Loss], n_api_errors: int) -> None:
    tally: Counter[str] = Counter(loss.loss_class for loss in losses)
    total = len(losses)
    print(f"\n{'=' * 64}")
    print("Loss classes — what shape did the winner's solution have?")
    print("=" * 64)
    for label, n in tally.most_common():
        pct = 100 * n / total if total else 0.0
        bar = "#" * int(pct / 2)
        print(f"  {label:<20} {n:>4}  ({pct:5.1f}%)  {bar}")
    if n_api_errors:
        print(f"  (CoW API errors: {n_api_errors})")


def _print_hops_histogram(losses: list[Loss]) -> None:
    with_receipts = [loss for loss in losses if loss.n_swaps >= 0]
    print(f"\n{'=' * 64}")
    print("Hops histogram — venue swap legs per winning settlement")
    print("=" * 64)
    if not with_receipts:
        print("  No receipt data (all receipts failed or --skip-receipts).")
        return
    buckets: Counter[str] = Counter()
    for loss in with_receipts:
        buckets["4+" if loss.n_swaps >= 4 else str(loss.n_swaps)] += 1
    total = len(with_receipts)
    for label in ("0", "1", "2", "3", "4+"):
        n = buckets.get(label, 0)
        pct = 100 * n / total if total else 0.0
        bar = "#" * int(pct / 2)
        print(f"  {label:>2} hops  {n:>4}  ({pct:5.1f}%)  {bar}")


def _print_venue_counter(venues: Counter[str], n_receipts: int, n_errors: int) -> None:
    print(f"\n{'=' * 64}")
    print(f"Venue counter — swap legs across {n_receipts} unique winning settlements")
    print("=" * 64)
    if not venues:
        print("  No venue data.")
    total = sum(venues.values())
    for venue, n in venues.most_common():
        pct = 100 * n / total if total else 0.0
        bar = "#" * int(pct / 2)
        print(f"  {venue:<16} {n:>4}  ({pct:5.1f}%)  {bar}")
    if n_errors:
        print(f"  (receipt fetch errors: {n_errors})")


def _print_worst(losses: list[Loss], skip_receipts: bool) -> None:
    print(f"\n{'=' * 64}")
    print("Worst 10 losses (lowest our/winner ratio)")
    print("=" * 64)
    header = f"  {'auction_id':>12} {'ratio':>7} {'class':<18}"
    if not skip_receipts:
        header += f" {'n_swaps':>7}  venues"
    print(header)
    print("  " + "-" * (len(header) + 8))
    for loss in losses[:10]:
        row = f"  {loss.auction_id:>12} {loss.ratio:>7.3f} {loss.loss_class:<18}"
        if not skip_receipts:
            swaps = str(loss.n_swaps) if loss.n_swaps >= 0 else "-"
            venue_s = ",".join(f"{v}x{n}" for v, n in loss.venues.most_common()) or "-"
            row += f" {swaps:>7}  {venue_s}"
        print(row)


def _print_unknown(unknown: Counter[str]) -> None:
    print(f"\n{'=' * 64}")
    print("Unknown venues — high-topic-count events we could not classify")
    print("=" * 64)
    if not unknown:
        print("  None — every swap-shaped log matched the venue table.")
        return
    print(f"  {'n':>4}  address + topic0 (paste address into Blockscout)")
    for key, n in unknown.most_common(15):
        print(f"  {n:>4}  {key}")


def _verdict(
    losses: list[Loss], venues: Counter[str], skip_receipts: bool
) -> str:
    if not losses:
        return "no real-margin losses in window — nothing to decompose."
    if skip_receipts or not venues:
        tally = Counter(loss.loss_class for loss in losses)
        label, n = tally.most_common(1)[0]
        return f"dominant winner shape: {label} ({n}/{len(losses)} losses; venue data skipped)"
    venue, n = venues.most_common(1)[0]
    total = sum(venues.values())
    return f"dominant winner edge: {venue} ({n}/{total} swap legs, {100 * n / total:.0f}%)"


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def run_analysis(
    hours: int, limit: int, ratio: float, skip_receipts: bool
) -> None:
    since = datetime.now(UTC) - timedelta(hours=hours)
    print(
        f"\nLoss decomposition  (last {hours}h, since {since:%Y-%m-%d %H:%M} UTC, "
        f"ratio<{ratio}, limit {limit}{', skip-receipts' if skip_receipts else ''})"
    )

    rows = await fetch_loss_rows(hours)
    losses = compute_losses(rows, ratio_threshold=ratio, limit=limit)
    print(f"  {len(rows)} feasible router-v2×winner rows → {len(losses)} real-margin losses")
    if not losses:
        print("\n  SUMMARY: " + _verdict(losses, Counter(), skip_receipts) + "\n")
        return

    # Step 2 — live CoW API: winner shape + settlement txHash per loss.
    n_api_errors = 0
    for loss in losses:
        try:
            comp = await fetch_competition(loss.auction_id)
        except Exception as exc:  # noqa: BLE001 — tolerate per-auction API failures
            log.warning("competition_fetch_failed", auction_id=loss.auction_id, error=str(exc))
            comp = None
        if comp is None:
            n_api_errors += 1
        else:
            winner = pick_winner_solution(comp)
            if winner is None:
                n_api_errors += 1
            else:
                orders = winner.get("orders") or []
                loss.n_orders = len(orders) if isinstance(orders, list) else 0
                loss.loss_class = classify_solution(orders if isinstance(orders, list) else [])
                loss.tx_hash = winner_tx_hash(comp, winner)
        await asyncio.sleep(API_THROTTLE_S)

    # Step 3 — on-chain receipts per UNIQUE txHash (a tx can settle several
    # of our lost auctions' orders; never fetch it twice).
    venue_total: Counter[str] = Counter()
    unknown_total: Counter[str] = Counter()
    n_receipt_errors = 0
    n_receipts = 0
    if not skip_receipts:
        w3 = Web3(Web3.HTTPProvider(settings.rpc_arbitrum))
        by_tx: dict[str, tuple[Counter[str], Counter[str]]] = {}
        unique_txs = {loss.tx_hash for loss in losses if loss.tx_hash}
        for tx_hash in sorted(unique_txs):
            try:
                views = await fetch_receipt_logs(w3, tx_hash)
            except Exception as exc:  # noqa: BLE001 — tolerate per-tx RPC failures
                log.warning("receipt_fetch_failed", tx_hash=tx_hash, error=str(exc))
                n_receipt_errors += 1
                continue
            venues, unknown = classify_receipt_logs(views)
            by_tx[tx_hash] = (venues, unknown)
            venue_total += venues
            unknown_total += unknown
            n_receipts += 1
            await asyncio.sleep(RECEIPT_THROTTLE_S)
        for loss in losses:
            if loss.tx_hash and loss.tx_hash in by_tx:
                loss.venues, _ = by_tx[loss.tx_hash]
                loss.n_swaps = sum(loss.venues.values())

    # Step 4 — print everything.
    _print_loss_classes(losses, n_api_errors)
    if not skip_receipts:
        _print_hops_histogram(losses)
        _print_venue_counter(venue_total, n_receipts, n_receipt_errors)
    _print_worst(losses, skip_receipts)
    if not skip_receipts:
        _print_unknown(unknown_total)

    print(f"\n  SUMMARY: {_verdict(losses, venue_total, skip_receipts)}\n")


async def main_async(hours: int, limit: int, ratio: float, skip_receipts: bool) -> None:
    try:
        await run_analysis(hours=hours, limit=limit, ratio=ratio, skip_receipts=skip_receipts)
    except Exception as exc:
        log.error(
            "analyze_losses_unhandled",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Decompose our real-margin losses: winner solution shape, "
        "AMM venues used, and the dominant edge we lack."
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=24,
        help="Look-back window in hours (default: 24).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=40,
        help="Max losses to decompose, worst-first (default: 40).",
    )
    parser.add_argument(
        "--ratio",
        type=float,
        default=0.99,
        help="Real-margin threshold: keep losses with our/winner < RATIO (default: 0.99).",
    )
    parser.add_argument(
        "--skip-receipts",
        action="store_true",
        help="API-only quick mode: skip on-chain receipts (no venue/hops data).",
    )
    args = parser.parse_args()
    asyncio.run(
        main_async(
            hours=args.hours,
            limit=args.limit,
            ratio=args.ratio,
            skip_receipts=args.skip_receipts,
        )
    )


if __name__ == "__main__":
    main()
