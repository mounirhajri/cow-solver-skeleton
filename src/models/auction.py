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
    """A CoW auction batch as delivered to /solve.

    Schema mirrors ``components.schemas.Auction`` in the CoW driver's
    solver-engine OpenAPI spec (see docs/cow-spec/solver-engine-openapi.yml).
    Fields the spec marks as required are mandatory here; the rest carry
    safe defaults so the model also accepts internally-constructed test
    auctions that omit driver-specific metadata.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    # Per spec ``id`` may be null for non-auction calls (e.g. quoting).
    id: str | None = None
    tokens: dict[str, Token]
    orders: list[Order]
    liquidity: list[dict[str, object]] = Field(default_factory=list)
    effective_gas_price: int = Field(alias="effectiveGasPrice", default=0)
    deadline: str | None = None
    # Pass-through field: spec-required, but we don't act on it yet (no JIT
    # orders emitted). Storing it keeps the model spec-compliant for any
    # future logic that wants to respect the surplus-capture allowlist.
    surplus_capturing_jit_order_owners: list[str] = Field(
        alias="surplusCapturingJitOrderOwners",
        default_factory=list,
    )
