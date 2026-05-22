from src.models.solution import Solution, Trade


def test_solution_serializes_to_cow_schema() -> None:
    sol = Solution(
        id=1,
        prices={"0x82af49447d8a07e3bd95bd0d56f35241523fbab1": 3500 * 10**18},
        trades=[
            Trade(
                kind="fulfillment",
                order_uid="0x" + "a" * 112,
                executed_amount=10**18,
            )
        ],
        interactions=[],
    )
    payload = sol.model_dump(by_alias=True, mode="json")
    assert payload["id"] == 1
    assert payload["trades"][0]["kind"] == "fulfillment"
    assert payload["trades"][0]["orderUid"] == "0x" + "a" * 112
    assert payload["trades"][0]["executedAmount"] == str(10**18)


def test_empty_solution_is_valid() -> None:
    sol = Solution(id=1, prices={}, trades=[], interactions=[])
    payload = sol.model_dump(by_alias=True, mode="json")
    assert payload["trades"] == []
