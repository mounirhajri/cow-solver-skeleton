from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FeePolicy(BaseModel):
    """Fee policy attached to an order. We pass through opaquely."""

    model_config = ConfigDict(extra="allow")


class Order(BaseModel):
    """A CoW order in an auction.

    Wire amounts (sellAmount, buyAmount) are decimal strings of base units
    in the JSON. We parse them as int.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    uid: str
    sell_token: str = Field(alias="sellToken")
    buy_token: str = Field(alias="buyToken")
    sell_amount: int = Field(alias="sellAmount")
    buy_amount: int = Field(alias="buyAmount")
    fee_policies: list[FeePolicy] = Field(alias="feePolicies", default_factory=list)
    valid_to: int = Field(alias="validTo")
    kind: Literal["sell", "buy"]
    owner: str
    partially_fillable: bool = Field(alias="partiallyFillable")
    class_: Literal["market", "limit", "liquidity"] = Field(alias="class")
