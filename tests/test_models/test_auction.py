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


def test_auction_accepts_null_id(sample_auction_dict: dict) -> None:
    # Per CoW solver-engine OpenAPI, ``id`` is nullable to support quote-only
    # requests where the driver is not running an auction but asks the solver
    # for token-price estimates. Missing or explicit-null ``id`` both
    # parse successfully and yield ``auction.id is None``.
    del sample_auction_dict["id"]
    auction = Auction.model_validate(sample_auction_dict)
    assert auction.id is None

    sample_auction_dict["id"] = None
    auction = Auction.model_validate(sample_auction_dict)
    assert auction.id is None


def test_auction_accepts_cow_driver_extras(sample_auction_dict: dict) -> None:
    """Driver sends ``effectiveGasPrice``, ``deadline``,
    ``surplusCapturingJitOrderOwners`` — verify all parse without error
    and unknown extras are tolerated (extra='ignore')."""
    sample_auction_dict["effectiveGasPrice"] = "1500000000"
    sample_auction_dict["deadline"] = "2026-06-01T12:00:00Z"
    sample_auction_dict["surplusCapturingJitOrderOwners"] = [
        "0x" + "a" * 40,
    ]
    sample_auction_dict["unknownFieldFromFutureDriver"] = {"foo": "bar"}
    auction = Auction.model_validate(sample_auction_dict)
    assert auction.effective_gas_price == 1500000000
    assert auction.deadline == "2026-06-01T12:00:00Z"
    assert auction.surplus_capturing_jit_order_owners == ["0x" + "a" * 40]


def test_auction_tokens_indexed_by_address(sample_auction_dict: dict) -> None:
    auction = Auction.model_validate(sample_auction_dict)
    weth = auction.tokens["0x82af49447d8a07e3bd95bd0d56f35241523fbab1"]
    assert weth.decimals == 18
    assert weth.symbol == "WETH"
