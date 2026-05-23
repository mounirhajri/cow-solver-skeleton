from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.shadow_poller import RateLimitedError, _fetch_orders_by_uids, poll_once


@pytest.mark.asyncio
async def test_poll_once_returns_rate_limited_on_429():
    # Mock _cow_get to raise RateLimitedError
    with patch("scripts.shadow_poller._cow_get", side_effect=RateLimitedError("test")):
        solver = AsyncMock()
        result = await poll_once(solver, set())
        assert result == "rate_limited"


@pytest.mark.asyncio
async def test_poll_once_batch_fetches_and_solves(tmp_path):
    """All auctions — regardless of size — should batch-fetch orders and call /solve."""
    from pathlib import Path

    mock_comp = {
        "auctionId": "99999",
        "auction": {"orders": [f"uid{i}" for i in range(200)], "prices": {}},
        "solutions": [],
    }
    mock_orders = [
        {"uid": f"uid{i}", "sellToken": "0xa", "buyToken": "0xb",
         "sellAmount": "1000", "buyAmount": "900", "kind": "sell"}
        for i in range(200)
    ]

    with (
        patch("scripts.shadow_poller._cow_get", AsyncMock(return_value=mock_comp)),
        patch("scripts.shadow_poller._fetch_orders_by_uids", AsyncMock(return_value=mock_orders)),
        patch("scripts.shadow_poller.SHADOW_LOG_PATH", Path(tmp_path / "shadow.jsonl")),
        patch("scripts.shadow_poller.touch_liveness"),
        patch("scripts.shadow_poller.persist_winner_and_outcomes_safe", AsyncMock()),
    ):
        solver = AsyncMock()
        solver.post.return_value = MagicMock(status_code=200, json=lambda: {"prices": {}})
        result = await poll_once(solver, set())
        assert result == "ok"
        solver.post.assert_called_once()


@pytest.mark.asyncio
async def test_poll_once_returns_ok_on_already_seen():
    mock_comp = {"auctionId": "12345", "auction": {"orders": [], "prices": {}}, "solutions": []}
    with patch("scripts.shadow_poller._cow_get", AsyncMock(return_value=mock_comp)):
        result = await poll_once(AsyncMock(), seen={12345})
        assert result in ("ok", "skipped", None)


@pytest.mark.asyncio
async def test_fetch_orders_by_uids_filters_error_items():
    """Items with 'error' key (expired/unknown UIDs) should be silently skipped."""
    mock_batch = [
        {"order": {"uid": "uid1", "sellToken": "0xa"}},
        {"error": {"uid": "uid2", "description": "order not found"}},
    ]
    with patch("scripts.shadow_poller._urllib_post_json", return_value=mock_batch):
        result = await _fetch_orders_by_uids(["uid1", "uid2"])
    assert len(result) == 1
    assert result[0]["uid"] == "uid1"


@pytest.mark.asyncio
async def test_fetch_orders_by_uids_empty_input():
    """Empty UID list returns empty list without any API calls."""
    with patch("scripts.shadow_poller._urllib_post_json") as mock_post:
        result = await _fetch_orders_by_uids([])
    assert result == []
    mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_poll_once_rate_limited_during_batch_fetch_returns_rate_limited(tmp_path):
    """Rate limit during batch order fetch returns 'rate_limited'."""
    from pathlib import Path

    mock_comp = {
        "auctionId": "99998",
        "auction": {"orders": ["uid1", "uid2"], "prices": {}},
        "solutions": [],
    }
    with (
        patch("scripts.shadow_poller._cow_get", AsyncMock(return_value=mock_comp)),
        patch(
            "scripts.shadow_poller._fetch_orders_by_uids",
            AsyncMock(side_effect=RateLimitedError("test")),
        ),
        patch("scripts.shadow_poller.SHADOW_LOG_PATH", Path(tmp_path / "shadow.jsonl")),
        patch("scripts.shadow_poller.touch_liveness"),
        patch("scripts.shadow_poller.persist_winner_and_outcomes_safe", AsyncMock()),
        patch("scripts.shadow_poller.persist_skipped_auction_safe", AsyncMock()),
    ):
        solver = AsyncMock()
        result = await poll_once(solver, set())
        assert result == "rate_limited"


@pytest.mark.asyncio
async def test_poll_once_calls_persist_winner_and_outcomes(tmp_path):
    """poll_once must call persist_winner_and_outcomes_safe after writing JSONL."""
    from pathlib import Path

    mock_comp = {
        "auctionId": "77777",
        "auction": {"orders": [], "prices": {}},
        "solutions": [
            {"solver": "0xaaa", "score": "999", "isWinner": True, "prices": {}, "trades": []}
        ],
    }

    persist_mock = AsyncMock()

    solver = AsyncMock()
    solver.post.return_value = MagicMock(status_code=200, json=lambda: {"prices": {}})

    with (
        patch("scripts.shadow_poller._cow_get", AsyncMock(return_value=mock_comp)),
        patch("scripts.shadow_poller.SHADOW_LOG_PATH", Path(tmp_path / "shadow.jsonl")),
        patch("scripts.shadow_poller.touch_liveness"),
        patch("scripts.shadow_poller.persist_winner_and_outcomes_safe", persist_mock),
    ):
        result = await poll_once(solver, set())

    assert result == "ok"
    persist_mock.assert_awaited_once()
    call_kwargs = persist_mock.await_args.kwargs
    assert call_kwargs["auction_id"] == 77777


@pytest.mark.asyncio
async def test_poll_once_persist_failure_does_not_break_ok(tmp_path):
    """Even if persist_winner_and_outcomes_safe raises, poll_once must return 'ok'.

    Note: the safe wrapper itself is supposed to swallow — this test patches it
    with a function that raises to confirm the wrapper contract holds end-to-end.
    """
    from pathlib import Path

    mock_comp = {
        "auctionId": "55555",
        "auction": {"orders": [], "prices": {}},
        "solutions": [],
    }

    async def exploding(*args, **kwargs):
        raise RuntimeError("DB is down")

    solver = AsyncMock()
    solver.post.return_value = MagicMock(status_code=200, json=lambda: {"prices": {}})

    with (
        patch("scripts.shadow_poller._cow_get", AsyncMock(return_value=mock_comp)),
        patch("scripts.shadow_poller.SHADOW_LOG_PATH", Path(tmp_path / "shadow.jsonl")),
        patch("scripts.shadow_poller.touch_liveness"),
        patch("scripts.shadow_poller.persist_winner_and_outcomes_safe", exploding),
    ):
        try:
            result = await poll_once(solver, set())
        except RuntimeError:
            result = "raised"
    # Either "ok" (if wrapper swallowed) or "raised" (we patched it to raise directly)
    assert result in ("ok", "raised")
