from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_serializer


class Trade(BaseModel):
    """An executed order in a solution."""

    model_config = ConfigDict(populate_by_name=True)

    kind: Literal["fulfillment", "jit"]
    # Serialises as ``order`` — the field name the solver-engine spec's
    # Fulfillment schema requires (NOT ``orderUid``; the driver's serde
    # rejects a trade missing ``order``). Validation still accepts the legacy
    # ``orderUid`` alias and the Python name ``order_uid`` so existing callers
    # and stored data keep parsing.
    order_uid: str = Field(
        validation_alias=AliasChoices("order", "orderUid", "order_uid"),
        serialization_alias="order",
    )
    executed_amount: int = Field(
        validation_alias=AliasChoices("executedAmount", "executed_amount"),
        serialization_alias="executedAmount",
    )

    @field_serializer("executed_amount")
    def serialize_amount(self, v: int) -> str:
        return str(v)


class Solution(BaseModel):
    """A CoW solution returned from /solve."""

    model_config = ConfigDict(populate_by_name=True)

    id: int
    prices: dict[str, int]
    trades: list[Trade]
    interactions: list[dict[str, object]]

    @field_serializer("prices")
    def serialize_prices(self, v: dict[str, int]) -> dict[str, str]:
        return {k: str(val) for k, val in v.items()}
