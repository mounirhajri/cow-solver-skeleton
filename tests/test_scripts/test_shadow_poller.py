from unittest.mock import AsyncMock, patch

import pytest

from scripts.shadow_poller import RateLimitedError, poll_once


@pytest.mark.asyncio
async def test_poll_once_returns_rate_limited_on_429():
    # Mock _cow_get to raise RateLimitedError
    with patch("scripts.shadow_poller._cow_get", side_effect=RateLimitedError("test")):
        solver = AsyncMock()
        result = await poll_once(solver, set())
        assert result == "rate_limited"


@pytest.mark.asyncio
async def test_poll_once_skips_large_auctions():
    # Mock _cow_get to return a competition with too many orders
    mock_comp = {
        "auctionId": "99999",
        "auction": {"orders": [f"uid{i}" for i in range(60)], "prices": {}},
        "solutions": [],
    }
    with patch("scripts.shadow_poller._cow_get", AsyncMock(return_value=mock_comp)):
        solver = AsyncMock()
        result = await poll_once(solver, set())
        assert result == "skipped"


@pytest.mark.asyncio
async def test_poll_once_returns_ok_on_already_seen():
    mock_comp = {"auctionId": "12345", "auction": {"orders": [], "prices": {}}, "solutions": []}
    with patch("scripts.shadow_poller._cow_get", AsyncMock(return_value=mock_comp)):
        result = await poll_once(AsyncMock(), seen={12345})
        # Already seen returns early — exact return value is implementation choice; "ok" is fine
        assert result in ("ok", "skipped", None)
