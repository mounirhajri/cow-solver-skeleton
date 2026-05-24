from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from src.log import get_logger

log = get_logger(__name__)

# Signing schemes that indicate a smart-contract wallet (EIP-1271 / pre-sign)
# rather than an EOA ECDSA signature.  Matched case-insensitively against the
# raw `signingScheme` field as it appears in the CoW auction JSON.  "presign"
# is on-chain approval — also non-ECDSA, also must be validated by the driver
# (not us), so we lump it under the same "smart-wallet-signed" bucket for
# downstream branching purposes.
_SMART_WALLET_SCHEMES: frozenset[str] = frozenset({"presign", "eip1271", "erc1271"})

# EOA / ECDSA-signed schemes — the "normal" path. Listing them explicitly so
# the unknown-scheme warning can distinguish "novel" from "well-known EOA".
_EOA_SCHEMES: frozenset[str] = frozenset({"eip712", "ethsign"})

# Module-level dedup set so the warning fires once per unknown scheme per
# process lifetime, not per Order instance. CoW auctions can carry the same
# scheme across thousands of orders; one log line per novel value is plenty.
_warned_unknown_schemes: set[str] = set()


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

    @field_validator("signing_scheme")
    @classmethod
    def _warn_unknown_signing_scheme(cls, v: str | None) -> str | None:
        """Log once per process lifetime when an unknown signing scheme appears.

        Soft failure mode otherwise: an unknown scheme silently maps to
        ``is_smart_wallet_signed = False`` (treated as EOA), and any
        downstream branching keyed on the flag would behave wrong without an
        observable signal. CoW could introduce a new scheme (or use a
        spelling we don't recognise) — better to log loudly than discover
        months later. See specs/2026-05-26-router-and-logging-followups.md §4.
        """
        if v is None:
            return v
        normalized = v.lower()
        if normalized in _SMART_WALLET_SCHEMES or normalized in _EOA_SCHEMES:
            return v
        if normalized not in _warned_unknown_schemes:
            _warned_unknown_schemes.add(normalized)
            log.warning("unknown_signing_scheme_observed", scheme=v)
        return v

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
