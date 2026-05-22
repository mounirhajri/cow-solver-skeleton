from pydantic import BaseModel, ConfigDict, Field

from src.models.order import Order


class Token(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    decimals: int
    symbol: str | None = None
    reference_price: int | None = Field(alias="referencePrice", default=None)
    available_balance: int = Field(alias="availableBalance", default=0)
    trusted: bool = False


class Auction(BaseModel):
    """A CoW auction batch as delivered to /solve."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    tokens: dict[str, Token]
    orders: list[Order]
    liquidity: list[dict] = Field(default_factory=list)
    effective_gas_price: int = Field(alias="effectiveGasPrice", default=0)
    deadline: str | None = None
