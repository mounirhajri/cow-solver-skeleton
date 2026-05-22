import json
from pathlib import Path

import pytest

from src.models.auction import Auction


@pytest.fixture
def sample_auction_dict() -> dict:
    path = Path(__file__).parent.parent / "fixtures" / "sample_auction.json"
    return json.loads(path.read_text())


def test_auction_parses_minimal_valid_input(sample_auction_dict: dict) -> None:
    auction = Auction.model_validate(sample_auction_dict)
    assert auction.id == "12345"
    assert len(auction.orders) == 1
    assert auction.orders[0].kind == "sell"
    assert auction.orders[0].sell_amount == 10**18


def test_auction_rejects_missing_id(sample_auction_dict: dict) -> None:
    del sample_auction_dict["id"]
    with pytest.raises(ValueError):
        Auction.model_validate(sample_auction_dict)


def test_auction_tokens_indexed_by_address(sample_auction_dict: dict) -> None:
    auction = Auction.model_validate(sample_auction_dict)
    weth = auction.tokens["0x82af49447d8a07e3bd95bd0d56f35241523fbab1"]
    assert weth.decimals == 18
    assert weth.symbol == "WETH"
