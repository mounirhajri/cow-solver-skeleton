"""Tests for the V4 Universal Router swap encoder.

Selectors are pinned against fresh keccak so a stray edit to the Solidity
signature string is caught at unit-test time rather than producing
calldata that routes to the wrong on-chain function (silent revert).

The golden test decodes every ABI layer of the produced ``execute``
calldata back — commands/inputs/deadline, then the (actions, params)
plan, then each per-action params blob — to verify the exact byte layout
the deployed Universal Router sees. This is the only way to catch
field-ordering bugs (e.g. accidentally adopting v4-periphery main's
``minHopPriceX36`` struct, which the DEPLOYED router does not have)
without a fork test.
"""

import pytest
from eth_abi import decode
from eth_utils import keccak

from src.encoder.erc20 import APPROVE_SELECTOR
from src.encoder.v4_calldata import (
    ACTION_SETTLE_ALL,
    ACTION_SWAP_EXACT_IN_SINGLE,
    ACTION_TAKE_ALL,
    COMMAND_V4_SWAP,
    EXECUTE_SELECTOR,
    HOOKS_NONE,
    PERMIT2_ADDRESS,
    PERMIT2_APPROVE_SELECTOR,
    UNIVERSAL_ROUTER_ARBITRUM,
    encode_execute,
    encode_v4_swap_input,
    encode_v4_swap_interactions,
    sort_currencies,
)

# Arbitrum mainnet addresses — pinned values so the tests double as
# documentation of which contracts we target. Note USDC (0xaf88…) is
# numerically GREATER than WETH (0x82aF…), so USDC→WETH is oneForZero.
USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"

_POOL_KEY_TYPE = "(address,address,uint24,int24,address)"
_SWAP_PARAMS_TYPE = f"({_POOL_KEY_TYPE},bool,uint128,uint128,bytes)"

DEADLINE = 1_900_000_000
AMOUNT_IN = 100_000_000  # 100 USDC
AMOUNT_OUT_MIN = 34_000_000_000_000  # wei WETH (on-chain guard, slippage below promise)
EXECUTED_BUY = 34_170_854_271_356  # wei WETH (the PROMISED amount = the quote)


def test_permit2_approve_selector_matches_keccak() -> None:
    sig = "approve(address,address,uint160,uint48)"
    assert keccak(text=sig)[:4] == PERMIT2_APPROVE_SELECTOR
    # Pin the literal too — catches an accidental edit to the signature
    # string (e.g. uint256 instead of uint160) that keccak alone would
    # happily propagate.
    assert PERMIT2_APPROVE_SELECTOR.hex() == "87517c45"


def test_execute_selector_matches_keccak() -> None:
    sig = "execute(bytes,bytes[],uint256)"
    assert keccak(text=sig)[:4] == EXECUTE_SELECTOR
    assert EXECUTE_SELECTOR.hex() == "3593564c"


def test_command_and_action_constants_pinned() -> None:
    # universal-router@b122a8d Commands.sol:35 / v4-periphery@4d85e04
    # Actions.sol:18,30,34 — the deployed dispatch tables.
    assert COMMAND_V4_SWAP == 0x10
    assert ACTION_SWAP_EXACT_IN_SINGLE == 0x06
    assert ACTION_SETTLE_ALL == 0x0C
    assert ACTION_TAKE_ALL == 0x0F


def test_sort_currencies_orders_numerically_both_directions() -> None:
    # WETH (0x82aF…) < USDC (0xaf88…) as integers.
    assert sort_currencies(USDC, WETH) == (WETH, USDC)
    assert sort_currencies(WETH, USDC) == (WETH, USDC)


def _decode_execute_layers(
    call_data: bytes,
) -> tuple[bytes, list[bytes], int]:
    """Peel the outermost layer: selector + (commands, inputs, deadline)."""
    assert call_data[:4] == EXECUTE_SELECTOR
    commands, inputs, deadline = decode(["bytes", "bytes[]", "uint256"], call_data[4:])
    return commands, list(inputs), deadline


def test_golden_decode_every_layer() -> None:
    """Build the full interaction list, then decode every ABI layer back
    and assert each field verbatim."""
    interactions = encode_v4_swap_interactions(
        sell_token=USDC,
        buy_token=WETH,
        fee=500,
        tick_spacing=10,
        amount_in=AMOUNT_IN,
        amount_out_minimum=AMOUNT_OUT_MIN,
        executed_buy=EXECUTED_BUY,
        deadline=DEADLINE,
    )
    assert len(interactions) == 3
    erc20_approve, permit2_approve, ur_execute = interactions

    # --- 1. ERC20 approve: sell_token grants Permit2 ---------------------
    assert erc20_approve.target == USDC
    assert erc20_approve.value == 0
    assert erc20_approve.call_data[:4] == APPROVE_SELECTOR
    spender, amount = decode(["address", "uint256"], erc20_approve.call_data[4:])
    assert spender.lower() == PERMIT2_ADDRESS.lower()
    assert amount == AMOUNT_IN
    # Approves move no tokens — empty flow, same convention as V3.
    assert erc20_approve.inputs == ()
    assert erc20_approve.outputs == ()

    # --- 2. Permit2 approve: settlement grants the Universal Router ------
    assert permit2_approve.target == PERMIT2_ADDRESS
    assert permit2_approve.value == 0
    assert permit2_approve.call_data[:4] == PERMIT2_APPROVE_SELECTOR
    token, spender, amount, expiration = decode(
        ["address", "address", "uint160", "uint48"], permit2_approve.call_data[4:]
    )
    assert token.lower() == USDC.lower()
    assert spender.lower() == UNIVERSAL_ROUTER_ARBITRUM.lower()
    assert amount == AMOUNT_IN
    assert expiration == DEADLINE
    assert permit2_approve.inputs == ()
    assert permit2_approve.outputs == ()

    # --- 3. execute(commands, inputs, deadline) --------------------------
    assert ur_execute.target == UNIVERSAL_ROUTER_ARBITRUM
    assert ur_execute.value == 0
    commands, inputs, deadline = _decode_execute_layers(ur_execute.call_data)
    assert commands == bytes([COMMAND_V4_SWAP])
    assert len(inputs) == 1
    assert deadline == DEADLINE

    # --- 3a. inputs[0] = abi.encode(actions, params) ----------------------
    actions, params = decode(["bytes", "bytes[]"], inputs[0])
    assert actions == bytes([ACTION_SWAP_EXACT_IN_SINGLE, ACTION_SETTLE_ALL, ACTION_TAKE_ALL])
    assert len(params) == 3

    # --- 3b. params[0] = ExactInputSingleParams ---------------------------
    (swap,) = decode([_SWAP_PARAMS_TYPE], params[0])
    pool_key, zero_for_one, amount_in, amount_out_min, hook_data = swap
    currency0, currency1, fee, tick_spacing, hooks = pool_key
    # WETH < USDC numerically → WETH is currency0; selling USDC = oneForZero.
    assert currency0.lower() == WETH.lower()
    assert currency1.lower() == USDC.lower()
    assert fee == 500
    assert tick_spacing == 10
    assert hooks.lower() == HOOKS_NONE.lower()
    assert zero_for_one is False
    assert amount_in == AMOUNT_IN
    assert amount_out_min == AMOUNT_OUT_MIN
    assert hook_data == b""

    # --- 3c. params[1] = SETTLE_ALL (currencyIn, maxAmount) ---------------
    settle_currency, settle_max = decode(["address", "uint256"], params[1])
    assert settle_currency.lower() == USDC.lower()
    assert settle_max == AMOUNT_IN

    # --- 3d. params[2] = TAKE_ALL (currencyOut, minAmount) ----------------
    take_currency, take_min = decode(["address", "uint256"], params[2])
    assert take_currency.lower() == WETH.lower()
    assert take_min == AMOUNT_OUT_MIN

    # --- Interaction token-flow accounting --------------------------------
    assert ur_execute.inputs == ((USDC, AMOUNT_IN),)
    # The DECLARED output is the promised executed_buy (driver solvency
    # accounting) — the slippage guard lives only inside the swap calldata.
    assert ur_execute.outputs == ((WETH, EXECUTED_BUY),)


def test_zero_for_one_true_when_selling_currency0() -> None:
    """Opposite direction of the golden test: selling WETH (currency0)
    must flip zeroForOne and swap the SETTLE/TAKE currencies."""
    blob = encode_v4_swap_input(
        sell_token=WETH,
        buy_token=USDC,
        fee=500,
        tick_spacing=10,
        amount_in=10**18,
        amount_out_minimum=2_900_000_000,
    )
    actions, params = decode(["bytes", "bytes[]"], blob)
    (swap,) = decode([_SWAP_PARAMS_TYPE], params[0])
    pool_key, zero_for_one, _, _, _ = swap
    # Pool key is direction-independent — same sorted pair either way.
    assert pool_key[0].lower() == WETH.lower()
    assert pool_key[1].lower() == USDC.lower()
    assert zero_for_one is True
    settle_currency, _ = decode(["address", "uint256"], params[1])
    take_currency, _ = decode(["address", "uint256"], params[2])
    assert settle_currency.lower() == WETH.lower()
    assert take_currency.lower() == USDC.lower()


def test_amount_in_above_uint128_raises() -> None:
    with pytest.raises(ValueError, match="uint128"):
        encode_v4_swap_input(
            sell_token=USDC,
            buy_token=WETH,
            fee=500,
            tick_spacing=10,
            amount_in=2**128,
            amount_out_minimum=0,
        )


def test_amount_out_minimum_above_uint128_raises() -> None:
    with pytest.raises(ValueError, match="uint128"):
        encode_v4_swap_input(
            sell_token=USDC,
            buy_token=WETH,
            fee=500,
            tick_spacing=10,
            amount_in=AMOUNT_IN,
            amount_out_minimum=2**128,
        )


def test_zero_amount_in_raises() -> None:
    with pytest.raises(ValueError, match="positive"):
        encode_v4_swap_input(
            sell_token=USDC,
            buy_token=WETH,
            fee=500,
            tick_spacing=10,
            amount_in=0,
            amount_out_minimum=0,
        )


def test_same_token_raises() -> None:
    with pytest.raises(ValueError, match="must differ"):
        encode_v4_swap_input(
            sell_token=USDC,
            buy_token=USDC.lower(),  # case difference must not evade the check
            fee=500,
            tick_spacing=10,
            amount_in=AMOUNT_IN,
            amount_out_minimum=0,
        )


def test_deadline_above_uint48_raises() -> None:
    # uint48 governs because the deadline doubles as the Permit2 expiration.
    with pytest.raises(ValueError, match="uint48"):
        encode_v4_swap_interactions(
            sell_token=USDC,
            buy_token=WETH,
            fee=500,
            tick_spacing=10,
            amount_in=AMOUNT_IN,
            amount_out_minimum=AMOUNT_OUT_MIN,
            executed_buy=EXECUTED_BUY,
            deadline=2**48,
        )


def test_execute_rejects_command_input_length_mismatch() -> None:
    with pytest.raises(ValueError, match="mismatch"):
        encode_execute(bytes([COMMAND_V4_SWAP]), [], DEADLINE)
