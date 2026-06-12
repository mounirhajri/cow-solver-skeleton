"""Tests for scripts/analyze_losses.py.

Only the pure pieces are tested — DB/API/chain access is factored behind thin
shells (``fetch_loss_rows`` / ``fetch_competition`` / ``fetch_receipt_logs``)
that are deliberately NOT exercised here (no network, no sqlite needed):

  * classify_solution      — winner shape from the v2 ``orders`` array,
  * classify_log / classify_receipt_logs — venue table incl. V4-by-address,
  * compute_losses         — dedupe + ratio filter + worst-first cap,
  * pick_winner_solution / winner_tx_hash — competition-body plumbing.
"""

from __future__ import annotations

from collections import Counter

from scripts.analyze_losses import (
    BALANCER_V3_VAULT,
    CLASS_COW_MATCH,
    CLASS_EMPTY,
    CLASS_MULTI,
    CLASS_SINGLE,
    UNISWAP_V4_POOL_MANAGER,
    classify_log,
    classify_receipt_logs,
    classify_solution,
    compute_losses,
    pick_winner_solution,
    winner_tx_hash,
)

TOKEN_A = "0x" + "aa" * 20
TOKEN_B = "0x" + "bb" * 20
TOKEN_C = "0x" + "cc" * 20

TOPIC_V3_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
TOPIC_V2_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
TOPIC_CURVE = "0x8b3e96f2b889fa771c53c981b40daf005f63f637f1869f707052d15a3dd97140"
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TOPIC_UNSEEN = "0x" + "12" * 32
PAD = "0x" + "00" * 32

POOL = "0x" + "33" * 20


def _uid(sell: str, buy: str) -> str:
    """Order UID: 0x + 32-byte hash + 20-byte sell token + 20-byte buy token."""
    return "0x" + "ff" * 32 + sell[2:] + buy[2:]


# ── classify_solution ────────────────────────────────────────────────────────


def test_cow_match_from_explicit_token_keys() -> None:
    orders = [
        {"sellToken": TOKEN_A, "buyToken": TOKEN_B},
        {"sellToken": TOKEN_B, "buyToken": TOKEN_A},
    ]
    assert classify_solution(orders) == CLASS_COW_MATCH


def test_cow_match_is_case_insensitive() -> None:
    orders = [
        {"sellToken": TOKEN_A.upper().replace("0X", "0x"), "buyToken": TOKEN_B},
        {"sellToken": TOKEN_B, "buyToken": TOKEN_A},
    ]
    assert classify_solution(orders) == CLASS_COW_MATCH


def test_cow_match_from_uid_fallback() -> None:
    # v2 orders entries without sellToken/buyToken — pair decoded from the UID.
    orders = [{"id": _uid(TOKEN_A, TOKEN_B)}, {"id": _uid(TOKEN_B, TOKEN_A)}]
    assert classify_solution(orders) == CLASS_COW_MATCH


def test_two_orders_same_direction_is_multi() -> None:
    orders = [
        {"sellToken": TOKEN_A, "buyToken": TOKEN_B},
        {"sellToken": TOKEN_A, "buyToken": TOKEN_B},
    ]
    assert classify_solution(orders) == CLASS_MULTI


def test_two_orders_different_pairs_is_multi() -> None:
    orders = [
        {"sellToken": TOKEN_A, "buyToken": TOKEN_B},
        {"sellToken": TOKEN_C, "buyToken": TOKEN_A},
    ]
    assert classify_solution(orders) == CLASS_MULTI


def test_single_and_empty_and_many() -> None:
    assert classify_solution([{"sellToken": TOKEN_A, "buyToken": TOKEN_B}]) == CLASS_SINGLE
    assert classify_solution([]) == CLASS_EMPTY
    assert classify_solution([{}, {}, {}]) == CLASS_MULTI


def test_two_garbage_orders_fall_back_to_multi() -> None:
    assert classify_solution(["not-a-dict", None]) == CLASS_MULTI
    assert classify_solution([{"id": "0xshort"}, {"id": 42}]) == CLASS_MULTI


# ── classify_log / classify_receipt_logs ─────────────────────────────────────


def test_v4_matched_by_poolmanager_address_even_with_unseen_topic() -> None:
    # The PRIMARY V4 rule: ANY event from the PoolManager singleton is V4
    # activity, regardless of topic (hooks may vary the event signature).
    assert classify_log(UNISWAP_V4_POOL_MANAGER, [TOPIC_UNSEEN, PAD, PAD]) == "uniswap-v4"
    # Checksummed address variants must match too.
    mixed = "0x360E68faCcca8cA495c1B759Fd9EEe466db9FB32"
    assert classify_log(mixed, [TOPIC_UNSEEN]) == "uniswap-v4"


def test_balancer_v3_matched_by_vault_address() -> None:
    assert classify_log(BALANCER_V3_VAULT, [TOPIC_UNSEEN]) == "balancer-v3"


def test_known_topics_classified_on_any_address() -> None:
    assert classify_log(POOL, [TOPIC_V3_SWAP, PAD, PAD]) == "uniswap-v3"
    assert classify_log(POOL, [TOPIC_V2_SWAP, PAD, PAD]) == "v2-style"
    assert classify_log(POOL, [TOPIC_CURVE, PAD]) == "curve-exchange"


def test_transfers_are_token_movement_not_venue() -> None:
    # ERC-20 Transfer has 3 topics — would hit the unknown bucket if not
    # explicitly ignored.
    assert classify_log(TOKEN_A, [TOPIC_TRANSFER, PAD, PAD]) is None


def test_unknown_high_topic_count_is_bucketed_low_count_ignored() -> None:
    venues, unknown = classify_receipt_logs(
        [
            (POOL, [TOPIC_UNSEEN, PAD, PAD]),  # 3 topics → unknown bucket
            (POOL, [TOPIC_UNSEEN, PAD]),  # 2 topics → plumbing, ignored
            (POOL, []),  # no topics → ignored
        ]
    )
    assert venues == Counter()
    assert sum(unknown.values()) == 1
    (key,) = unknown
    assert POOL in key  # address present → Blockscout-able
    assert TOPIC_UNSEEN[:10] in key  # topic prefix present


def test_classify_receipt_logs_full_mix() -> None:
    venues, unknown = classify_receipt_logs(
        [
            (TOKEN_A, [TOPIC_TRANSFER, PAD, PAD]),
            (POOL, [TOPIC_V3_SWAP, PAD, PAD]),
            (UNISWAP_V4_POOL_MANAGER, [TOPIC_UNSEEN, PAD, PAD]),
            (UNISWAP_V4_POOL_MANAGER, [TOPIC_UNSEEN, PAD, PAD]),
            (POOL, [TOPIC_CURVE, PAD]),
            ("0x" + "44" * 20, [TOPIC_UNSEEN, PAD, PAD]),
        ]
    )
    assert venues == Counter({"uniswap-v4": 2, "uniswap-v3": 1, "curve-exchange": 1})
    assert sum(unknown.values()) == 1


# ── compute_losses ───────────────────────────────────────────────────────────


def test_compute_losses_ratio_filter() -> None:
    rows = [
        (1, 98, 100),  # 0.98  < 0.99 → loss
        (2, 99, 100),  # 0.99 not < 0.99 → kept out (boundary)
        (3, 100, 100),  # win → out
        (4, 120, 100),  # win → out
    ]
    losses = compute_losses(rows, ratio_threshold=0.99, limit=40)
    assert [loss.auction_id for loss in losses] == [1]
    assert losses[0].ratio == 0.98


def test_compute_losses_dedupes_keeping_our_best_score() -> None:
    # Two router-v2 rows for auction 7 — the honest margin uses our BEST score.
    rows = [(7, 50, 100), (7, 90, 100)]
    losses = compute_losses(rows, ratio_threshold=0.99, limit=40)
    assert len(losses) == 1
    assert losses[0].our_score == 90
    # And if the best one is no longer a real-margin loss, the auction drops out.
    rows = [(8, 50, 100), (8, 100, 100)]
    assert compute_losses(rows, ratio_threshold=0.99, limit=40) == []


def test_compute_losses_sorts_worst_first_and_caps_limit() -> None:
    rows = [(1, 90, 100), (2, 10, 100), (3, 50, 100)]
    losses = compute_losses(rows, ratio_threshold=0.99, limit=2)
    assert [loss.auction_id for loss in losses] == [2, 3]


def test_compute_losses_skips_missing_or_zero_winner() -> None:
    rows = [(1, 90, None), (2, None, 100), (3, 90, 0), (4, 5, 100)]
    losses = compute_losses(rows, ratio_threshold=0.99, limit=40)
    assert [loss.auction_id for loss in losses] == [4]


def test_compute_losses_accepts_numeric_strings() -> None:
    # Numeric(40,0) columns can surface as Decimal/str depending on driver.
    rows = [(1, "98", "100")]
    losses = compute_losses(rows, ratio_threshold=0.99, limit=40)
    assert losses[0].our_score == 98 and losses[0].winner_score == 100


# ── pick_winner_solution / winner_tx_hash ────────────────────────────────────


def test_pick_winner_prefers_is_winner_flag() -> None:
    comp = {
        "solutions": [
            {"solverAddress": "0x1", "isWinner": False, "ranking": 2},
            {"solverAddress": "0x2", "isWinner": True, "ranking": 1},
        ]
    }
    winner = pick_winner_solution(comp)
    assert winner is not None and winner["solverAddress"] == "0x2"


def test_pick_winner_falls_back_to_ranking_1() -> None:
    comp = {"solutions": [{"ranking": 3}, {"ranking": 1, "solverAddress": "0x9"}]}
    winner = pick_winner_solution(comp)
    assert winner is not None and winner["solverAddress"] == "0x9"


def test_pick_winner_tolerates_garbage_and_absence() -> None:
    assert pick_winner_solution({}) is None
    assert pick_winner_solution({"solutions": "nope"}) is None
    assert pick_winner_solution({"solutions": [None, "x", {"ranking": 5}]}) is None


def test_winner_tx_hash_from_solution_then_top_level_fallback() -> None:
    tx = "0x" + "Ab" * 32
    assert winner_tx_hash({}, {"txHash": tx}) == tx.lower()
    assert winner_tx_hash({"transactionHashes": [tx]}, {}) == tx.lower()
    assert winner_tx_hash({"transactionHashes": []}, {"txHash": None}) is None
    assert winner_tx_hash({}, {"txHash": "not-hex"}) is None
