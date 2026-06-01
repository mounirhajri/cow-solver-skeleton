"""GPv2Settlement.settle() calldata encoder.

Builds the real settlement calldata for a solver solution so we can
``eth_call`` it and prove on-chain feasibility (token conservation + signature
validity), rather than trusting our self-reported CIP-14 score.

settle() signature:
    settle(
        address[]   tokens,
        uint256[]   clearingPrices,
        Trade[]     trades,
        Interaction[][3] interactions   // [pre, intra, post]
    )

Trade tuple:
    (uint256 sellTokenIndex, uint256 buyTokenIndex, address receiver,
     uint256 sellAmount, uint256 buyAmount, uint32 validTo, bytes32 appData,
     uint256 feeAmount, uint256 flags, uint256 executedAmount, bytes signature)
"""

from __future__ import annotations

# Signing scheme index -> bits 5-6 of the trade flags bitfield.
# Order matches cowprotocol GPv2Signing.Scheme enum: eip712, ethsign, eip1271, presign.
_SCHEME_INDEX = {"eip712": 0, "ethsign": 1, "eip1271": 2, "presign": 3}

# Sell-token balance source -> bits 2-3.
_SELL_BALANCE = {"erc20": 0x00, "external": 0x08, "internal": 0x0C}
# Buy-token balance source -> bit 4.
_BUY_BALANCE = {"erc20": 0x00, "internal": 0x10}


def encode_trade_flags(
    kind: str,
    partially_fillable: bool,
    signing_scheme: str,
    sell_token_balance: str = "erc20",
    buy_token_balance: str = "erc20",
) -> int:
    """Pack a GPv2 trade ``flags`` bitfield.

    Bit layout (see module docstring / cowprotocol GPv2Trade):
      bit 0    kind (sell=0, buy=1)
      bit 1    partiallyFillable
      bits 2-3 sellTokenBalance
      bit 4    buyTokenBalance
      bits 5-6 signingScheme
    """
    scheme = signing_scheme.lower()
    if scheme not in _SCHEME_INDEX:
        raise ValueError(f"unknown signing scheme: {signing_scheme!r}")
    flags = 0
    flags |= 0 if kind.lower() == "sell" else 0x01
    flags |= 0x02 if partially_fillable else 0
    # Unrecognised balance source falls back to erc20 (0x00) by design:
    # callers pass validated values and our orders are almost always erc20.
    flags |= _SELL_BALANCE.get(sell_token_balance.lower(), 0x00)
    flags |= _BUY_BALANCE.get(buy_token_balance.lower(), 0x00)
    flags |= _SCHEME_INDEX[scheme] << 5
    return flags
