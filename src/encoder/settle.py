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

from dataclasses import dataclass

from eth_abi import decode, encode
from eth_utils import keccak

_SETTLE_SIG = (
    "settle(address[],uint256[],"
    "(uint256,uint256,address,uint256,uint256,uint32,bytes32,uint256,uint256,uint256,bytes)[],"
    "(address,uint256,bytes)[][3])"
)
SETTLE_SELECTOR = keccak(text=_SETTLE_SIG)[:4]

_TRADE_TYPE = (
    "(uint256,uint256,address,uint256,uint256,uint32,bytes32,uint256,uint256,uint256,bytes)"
)
_INTERACTION_TYPE = "(address,uint256,bytes)"


@dataclass(frozen=True)
class SettleTrade:
    """One GPv2Trade.Data row for a settle() call."""

    sell_token_index: int
    buy_token_index: int
    receiver: str
    sell_amount: int
    buy_amount: int
    valid_to: int
    app_data: bytes      # exactly 32 bytes
    fee_amount: int
    flags: int
    executed_amount: int
    signature: bytes

    def as_tuple(self) -> tuple[object, ...]:
        return (
            self.sell_token_index,
            self.buy_token_index,
            self.receiver,
            self.sell_amount,
            self.buy_amount,
            self.valid_to,
            self.app_data,
            self.fee_amount,
            self.flags,
            self.executed_amount,
            self.signature,
        )


def encode_settle(
    tokens: list[str],
    clearing_prices: list[int],
    trades: list[SettleTrade],
    intra_interactions: list[tuple[str, int, bytes]],
) -> bytes:
    """ABI-encode a GPv2Settlement.settle() call.

    ``intra_interactions`` is a list of (target, value, callData) tuples placed
    in the intra slot of the [pre, intra, post] interactions array; pre and post
    are always empty for our AMM-only settlement shape. Each tuple is converted
    to a list before encoding because ``eth_abi`` treats the interaction struct
    positionally and accepts either, but lists keep the nested-array shape
    explicit.
    """
    interactions = [[], [list(i) for i in intra_interactions], []]
    args = encode(
        _SETTLE_ARG_TYPES,
        [
            tokens,
            clearing_prices,
            [t.as_tuple() for t in trades],
            interactions,
        ],
    )
    return SETTLE_SELECTOR + args


_SETTLE_ARG_TYPES = [
    "address[]",
    "uint256[]",
    f"{_TRADE_TYPE}[]",
    f"{_INTERACTION_TYPE}[][3]",
]


@dataclass(frozen=True)
class DecodedSettle:
    """A settle() call recovered from raw calldata via canonical ABI decoding.

    ``intra_interactions`` mirrors ``encode_settle``'s input shape; ``pre`` and
    ``post`` are surfaced separately so a fidelity check can refuse to compare
    against real settlements that use hooks our AMM-only encoder doesn't model.
    """

    tokens: list[str]
    clearing_prices: list[int]
    trades: list[SettleTrade]
    pre_interactions: list[tuple[str, int, bytes]]
    intra_interactions: list[tuple[str, int, bytes]]
    post_interactions: list[tuple[str, int, bytes]]


def decode_settle(calldata: bytes) -> DecodedSettle:
    """Inverse of ``encode_settle``: parse settle() calldata back into fields.

    Uses canonical ``eth_abi.decode`` (independent of our hand-rolled encoder),
    so feeding our encoder's output through here and re-encoding is a genuine
    ABI-fidelity gate: any offset/padding/ordering bug breaks byte-equality.

    Raises ``ValueError`` if the 4-byte selector isn't settle()'s.
    """
    if calldata[:4] != SETTLE_SELECTOR:
        raise ValueError(
            f"selector mismatch: {calldata[:4].hex()} != {SETTLE_SELECTOR.hex()}"
        )
    tokens, prices, trades_raw, interactions = decode(_SETTLE_ARG_TYPES, calldata[4:])

    trades = [
        SettleTrade(
            sell_token_index=t[0],
            buy_token_index=t[1],
            receiver=t[2],
            sell_amount=t[3],
            buy_amount=t[4],
            valid_to=t[5],
            app_data=t[6],
            fee_amount=t[7],
            flags=t[8],
            executed_amount=t[9],
            signature=t[10],
        )
        for t in trades_raw
    ]
    pre, intra, post = (
        [(i[0], i[1], i[2]) for i in slot] for slot in interactions
    )
    return DecodedSettle(
        tokens=list(tokens),
        clearing_prices=list(prices),
        trades=trades,
        pre_interactions=pre,
        intra_interactions=intra,
        post_interactions=post,
    )


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
