from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class Trade(BaseModel):
    """An executed order in a solution."""

    model_config = ConfigDict(populate_by_name=True)

    kind: Literal["fulfillment", "jit"]
    order_uid: str = Field(alias="orderUid")
    executed_amount: int = Field(alias="executedAmount")

    @field_serializer("executed_amount")
    def serialize_amount(self, v: int) -> str:
        return str(v)


class Solution(BaseModel):
    """A CoW solution returned from /solve."""

    model_config = ConfigDict(populate_by_name=True)

    id: int
    prices: dict[str, int]
    trades: list[Trade]
    interactions: list[dict]

    @field_serializer("prices")
    def serialize_prices(self, v: dict[str, int]) -> dict[str, str]:
        return {k: str(val) for k, val in v.items()}
