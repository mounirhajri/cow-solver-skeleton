import pytest

from edge.pool_indexer import LongTailRouter
from src.models.auction import Auction
from src.solver.base import NoSolution


@pytest.mark.asyncio
async def test_stub_returns_no_solution():
    router = LongTailRouter()
    auction = Auction(
        id="1",
        tokens={},
        orders=[],
        liquidity=[],
        effectiveGasPrice=0,
        deadline=None,
    )
    result = await router.solve(auction)
    assert isinstance(result, NoSolution)


def test_stub_name_set():
    assert LongTailRouter.name == "long-tail-stub"
