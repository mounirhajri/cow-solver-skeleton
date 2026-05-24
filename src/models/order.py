from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

# Signing schemes that indicate a smart-contract wallet (EIP-1271 / pre-sign)
# rather than an EOA ECDSA signature.  Matched case-insensitively against the
# raw `signingScheme` field as it appears in the CoW auction JSON.  "presign"
# is on-chain approval — also non-ECDSA, also must be validated by the driver
# (not us), so we lump it under the same "smart-wallet-signed" bucket for
# downstream branching purposes.
_SMART_WALLET_SCHEMES: frozenset[str] = frozenset({"presign", "eip1271", "erc1271"})


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
    # Optional — older fixtures and EOA-only auctions omit it entirely.  We
    # tolerate the field being absent and default to None (treated as EOA).
    signing_scheme: str | None = Field(alias="signingScheme", default=None)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_smart_wallet_signed(self) -> bool:
        """True iff the order was signed by a smart-contract wallet.

        Used downstream to skip checks that would require a per-auction
        on-chain ``isValidSignature`` call (EIP-1271) or an approval lookup
        (pre-sign).  We do NOT validate the signature locally — the CoW
        Driver owns that.  This flag is purely an observability + routing
        hint for strategies.
        """
        if self.signing_scheme is None:
            return False
        return self.signing_scheme.lower() in _SMART_WALLET_SCHEMES
