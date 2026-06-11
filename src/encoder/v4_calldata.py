"""Uniswap V4 swap encoders — Universal Router + Permit2 path.

V4 swaps cannot call the PoolManager directly: it is locked behind the
unlock-callback pattern, so an EOA-style caller (our GPv2 settlement) must
go through a periphery router. We use the **Universal Router** ("URv2",
the V4-capable deployment) which pulls input tokens from the caller via
**Permit2** — there is no plain-ERC20 pull path for external payers.

A settlement therefore needs THREE interactions per V4 exact-input swap:

  1. ``sell_token.approve(PERMIT2, amount_in)``
     ERC20 allowance so Permit2's ``transferFrom`` can move the sell token
     out of the settlement. Same rationale as the V3 approve in
     ``erc20.py`` — the settlement holds no standing allowances and can
     only grant them from inside ``settle()``.

  2. ``PERMIT2.approve(sell_token, UNIVERSAL_ROUTER, amount_in, deadline)``
     Permit2-level allowance. When the router settles the swap debt it
     calls ``PERMIT2.transferFrom(payer, poolManager, amount, token)``
     with ``msg.sender == router``, and Permit2 checks
     ``allowance[payer][token][router]`` — so the spender here is the
     Universal Router, not the PoolManager.
     (universal-router@b122a8d contracts/modules/Permit2Payments.sol:42-45
     ``payOrPermit2Transfer``: payer != address(this) →
     ``permit2TransferFrom(token, payer, recipient, amount)`` which is
     ``PERMIT2.transferFrom(from, to, amount, token)``, lines 20-22.)

  3. ``UNIVERSAL_ROUTER.execute(commands, inputs, deadline)`` with a
     single ``V4_SWAP`` command carrying the action plan
     ``[SWAP_EXACT_IN_SINGLE, SETTLE_ALL, TAKE_ALL]``.

Output delivery — why no explicit recipient appears anywhere:
``TAKE_ALL`` decodes ``(Currency, uint256 minAmount)`` and pays out via
``_take(currency, msgSender(), amount)``
(v4-periphery@4d85e04 src/V4Router.sol:58-62). In the Universal Router
``msgSender()`` returns the reentrancy locker (universal-router@b122a8d
contracts/base/Dispatcher.sol:37-39), and the locker is set to
``msg.sender`` of ``execute`` by the ``isNotLocked`` modifier
(contracts/base/Lock.sol:14-25). The settlement calls ``execute``, so
``msgSender() == settlement`` and the swap output lands back at the
settlement contract. ``SETTLE_ALL`` symmetrically pays the input debt
from ``msgSender()`` (V4Router.sol:52-56), which is what interaction 2
authorises.

All constants below were verified against the sources the DEPLOYED
Arbitrum router was built from, not ``main``: deploy-addresses commit
universal-router@b122a8d (records ``UniversalRouterV2`` =
0xa51afafe…81a3 in deploy-addresses/arbitrum.json) pins
v4-periphery@4d85e04, which pins v4-core@b619b67. NOTE: v4-periphery
``main`` has since added a ``minHopPriceX36`` field to
``ExactInputSingleParams`` — that struct does NOT match the deployed
router; do not "update" this encoder from main without re-checking the
deployment pin.

Selectors are precomputed module-level constants — they never change for
a given Solidity signature and recomputing them per call adds keccak
overhead for no reason.
"""

from __future__ import annotations

from eth_abi import encode
from eth_utils import keccak

from src.encoder.erc20 import encode_approve
from src.encoder.interactions import Interaction

# ---------------------------------------------------------------------------
# Addresses (verified against developers.uniswap.org/contracts/v4/deployments,
# "Arbitrum One" section, 2026-06-11; checksummed via eth_utils).
# ---------------------------------------------------------------------------

# Universal Router ("UniversalRouterV2", the V4-capable deployment) on
# Arbitrum One. Also recorded in universal-router@b122a8d
# deploy-addresses/arbitrum.json as "UniversalRouterV2".
UNIVERSAL_ROUTER_ARBITRUM = "0xA51afAFe0263b40EdaEf0Df8781eA9aa03E381a3"

# Canonical Permit2 — same CREATE2 address on every chain.
PERMIT2_ADDRESS = "0x000000000022D473030F116dDEE9F6B43aC78BA3"

# ---------------------------------------------------------------------------
# Selectors. Solidity signatures kept verbatim so a grep across this module
# and the router source finds matching artefacts.
# ---------------------------------------------------------------------------

# permit2@cc56ad0 src/interfaces/IAllowanceTransfer.sol:123
#   function approve(address token, address spender, uint160 amount,
#                    uint48 expiration) external;
_SIG_PERMIT2_APPROVE = "approve(address,address,uint160,uint48)"

# keccak256("approve(address,address,uint160,uint48)")[:4] == 0x87517c45.
PERMIT2_APPROVE_SELECTOR = keccak(text=_SIG_PERMIT2_APPROVE)[:4]

# universal-router@b122a8d contracts/UniversalRouter.sol:35
#   function execute(bytes calldata commands, bytes[] calldata inputs,
#                    uint256 deadline) external payable
_SIG_EXECUTE = "execute(bytes,bytes[],uint256)"

# keccak256("execute(bytes,bytes[],uint256)")[:4] == 0x3593564c.
EXECUTE_SELECTOR = keccak(text=_SIG_EXECUTE)[:4]

# ---------------------------------------------------------------------------
# Universal Router command / V4 action bytes.
# ---------------------------------------------------------------------------

# universal-router@b122a8d contracts/libraries/Commands.sol:35
#   uint256 constant V4_SWAP = 0x10;
# Dispatched at contracts/base/Dispatcher.sol:248-250 — the command's input
# bytes are handed verbatim to BaseActionsRouter._executeActions.
COMMAND_V4_SWAP = 0x10

# v4-periphery@4d85e04 src/libraries/Actions.sol:18,30,34
#   uint256 internal constant SWAP_EXACT_IN_SINGLE = 0x06;
#   uint256 internal constant SETTLE_ALL           = 0x0c;
#   uint256 internal constant TAKE_ALL             = 0x0f;
ACTION_SWAP_EXACT_IN_SINGLE = 0x06
ACTION_SETTLE_ALL = 0x0C
ACTION_TAKE_ALL = 0x0F

# ---------------------------------------------------------------------------
# ABI shapes.
# ---------------------------------------------------------------------------

# v4-core@b619b67 src/types/PoolKey.sol:11-21:
#   struct PoolKey { Currency currency0; Currency currency1; uint24 fee;
#                    int24 tickSpacing; IHooks hooks; }
# Currency and IHooks are address-typed wrappers, so the ABI is plain
# addresses.
_POOL_KEY_STRUCT = "(address,address,uint24,int24,address)"

# v4-periphery@4d85e04 src/interfaces/IV4Router.sol:18-24:
#   struct ExactInputSingleParams { PoolKey poolKey; bool zeroForOne;
#       uint128 amountIn; uint128 amountOutMinimum; bytes hookData; }
# (No minHopPriceX36 — that field only exists on main, see module docstring.)
# Decoded on-chain by CalldataDecoder.decodeSwapExactInSingleParams
# (v4-periphery@4d85e04 src/libraries/CalldataDecoder.sol:196-210), which
# reads a leading offset word — i.e. it expects exactly the layout
# ``abi.encode(struct)`` produces for a dynamic struct.
_EXACT_INPUT_SINGLE_STRUCT = f"({_POOL_KEY_STRUCT},bool,uint128,uint128,bytes)"

# Pools without hooks use the zero address, and we never route hooked pools.
HOOKS_NONE = "0x" + "00" * 20

_UINT128_MAX = 2**128 - 1
_UINT48_MAX = 2**48 - 1
_UINT24_MAX = 2**24 - 1
_INT24_MIN, _INT24_MAX = -(2**23), 2**23 - 1


def sort_currencies(token_a: str, token_b: str) -> tuple[str, str]:
    """Order two tokens as V4 ``(currency0, currency1)``.

    PoolKey requires ``currency0 < currency1`` numerically (v4-core@b619b67
    src/types/PoolKey.sol:12-15 — "sorted numerically"). Hex addresses
    compare by integer value, which case-insensitive string comparison
    would get wrong, so compare as ints.
    """
    if int(token_a, 16) < int(token_b, 16):
        return token_a, token_b
    return token_b, token_a


def encode_permit2_approve(
    token: str,
    spender: str,
    amount: int,
    expiration: int,
) -> bytes:
    """Encode ``IAllowanceTransfer.approve(token, spender, amount, expiration)``.

    ``amount`` is uint160, ``expiration`` uint48 — Permit2 treats the
    allowance as valid while ``block.timestamp <= expiration``, so passing
    the swap deadline keeps the two windows aligned. Like ERC20 approve
    this overwrites rather than increments.
    """
    if not 0 <= amount <= _UINT128_MAX:
        # uint160 on-chain, but we never grant more than a swap's uint128
        # amountIn — tighter bound, same guard as the swap itself.
        raise ValueError(f"amount must fit uint128, got {amount}")
    if not 0 <= expiration <= _UINT48_MAX:
        raise ValueError(f"expiration must fit uint48, got {expiration}")
    return PERMIT2_APPROVE_SELECTOR + encode(
        ["address", "address", "uint160", "uint48"],
        [token, spender, amount, expiration],
    )


def encode_v4_swap_input(
    *,
    sell_token: str,
    buy_token: str,
    fee: int,
    tick_spacing: int,
    amount_in: int,
    amount_out_minimum: int,
) -> bytes:
    """Encode the ``V4_SWAP`` command input: ``abi.encode(actions, params)``.

    The Dispatcher hands these bytes to ``_executeActions`` →
    ``poolManager.unlock`` → ``_unlockCallback``, which decodes them as
    ``abi.encode(bytes actions, bytes[] params)`` (v4-periphery@4d85e04
    src/base/BaseActionsRouter.sol:31-36). The decoder enforces *strict*
    canonical ABI encoding (src/libraries/CalldataDecoder.sol:25-40);
    ``eth_abi.encode`` emits exactly that.

    Action plan and per-action params (V4Router._handleAction,
    v4-periphery@4d85e04 src/V4Router.sol):

      SWAP_EXACT_IN_SINGLE → abi.encode(ExactInputSingleParams)  (:38-40)
      SETTLE_ALL → abi.encode(currencyIn, maxAmount); router pays the
        full swap debt from msgSender, reverting if it exceeds
        ``maxAmount`` — amount_in is exact for exact-input, so the cap is
        amount_in itself  (:52-56, decodeCurrencyAndUint256)
      TAKE_ALL → abi.encode(currencyOut, minAmount); pays the full credit
        to msgSender, reverting below ``minAmount``  (:58-62)
    """
    if not 0 < amount_in <= _UINT128_MAX:
        raise ValueError(f"amount_in must fit uint128 and be positive, got {amount_in}")
    if not 0 <= amount_out_minimum <= _UINT128_MAX:
        raise ValueError(f"amount_out_minimum must fit uint128, got {amount_out_minimum}")
    if not 0 <= fee <= _UINT24_MAX:
        raise ValueError(f"fee must fit uint24, got {fee}")
    if not _INT24_MIN <= tick_spacing <= _INT24_MAX:
        raise ValueError(f"tick_spacing must fit int24, got {tick_spacing}")
    if int(sell_token, 16) == int(buy_token, 16):
        raise ValueError("sell_token and buy_token must differ")

    currency0, currency1 = sort_currencies(sell_token, buy_token)
    zero_for_one = int(sell_token, 16) == int(currency0, 16)

    actions = bytes([ACTION_SWAP_EXACT_IN_SINGLE, ACTION_SETTLE_ALL, ACTION_TAKE_ALL])
    swap_params = encode(
        [_EXACT_INPUT_SINGLE_STRUCT],
        [
            (
                (currency0, currency1, fee, tick_spacing, HOOKS_NONE),
                zero_for_one,
                amount_in,
                amount_out_minimum,
                b"",  # hookData — we never route hooked pools
            )
        ],
    )
    settle_params = encode(["address", "uint256"], [sell_token, amount_in])
    take_params = encode(["address", "uint256"], [buy_token, amount_out_minimum])

    return encode(
        ["bytes", "bytes[]"],
        [actions, [swap_params, settle_params, take_params]],
    )


def encode_execute(commands: bytes, inputs: list[bytes], deadline: int) -> bytes:
    """Encode ``UniversalRouter.execute(commands, inputs, deadline)`` calldata.

    One command byte per ``inputs`` element — the router reverts
    ``LengthMismatch`` otherwise (universal-router@b122a8d
    contracts/UniversalRouter.sol:44-48).
    """
    if len(commands) != len(inputs):
        raise ValueError(f"commands/inputs length mismatch: {len(commands)} != {len(inputs)}")
    return EXECUTE_SELECTOR + encode(
        ["bytes", "bytes[]", "uint256"],
        [commands, inputs, deadline],
    )


def encode_v4_swap_interactions(
    *,
    sell_token: str,
    buy_token: str,
    fee: int,
    tick_spacing: int,
    amount_in: int,
    amount_out_minimum: int,
    executed_buy: int,
    deadline: int,
    universal_router: str = UNIVERSAL_ROUTER_ARBITRUM,
    permit2: str = PERMIT2_ADDRESS,
) -> list[Interaction]:
    """Build the ordered interaction list for one V4 exact-input swap.

    Returns ``[erc20_approve, permit2_approve, ur_execute]`` — order
    matters: each grant must exist before the next call consumes it.
    Keyword-only — too many similarly-typed ints in one signature to
    trust positional order.

    Token-flow accounting mirrors ``encode_v3_swap``: the approves move
    no tokens themselves so they carry empty ``inputs``/``outputs``; the
    ``execute`` interaction consumes ``amount_in`` of ``sell_token`` from
    the settlement and returns ``buy_token`` to it (TAKE_ALL →
    msgSender == settlement, see module docstring).

    ``executed_buy`` vs ``amount_out_minimum``: the DECLARED interaction
    output must be the PROMISED amount (``executed_buy`` — what the
    clearing prices pay the trader), exactly like ``encode_v3_swap``
    declares ``executed_buy``. The slippage-reduced
    ``amount_out_minimum`` exists only INSIDE the swap calldata as the
    on-chain revert guard. Declaring the minimum instead would make the
    solution's token flows look insolvent to the CoW driver (declared
    inflow < trade payout) and risk whole-solution rejection — the
    2026-06-08 non-conformance failure mode.

    ``deadline`` doubles as the Permit2 allowance expiration — both are
    inclusive upper bounds on ``block.timestamp``, so the allowance lives
    exactly as long as the router would accept the swap.
    """
    if not 0 <= deadline <= _UINT48_MAX:
        # uint256 in execute(), but uint48 in Permit2 — the tighter bound
        # governs. Any real timestamp fits uint48 until year ~8.9 million.
        raise ValueError(f"deadline must fit uint48, got {deadline}")

    erc20_approve = Interaction(
        target=sell_token,
        value=0,
        call_data=encode_approve(permit2, amount_in),
    )
    permit2_approve = Interaction(
        target=permit2,
        value=0,
        call_data=encode_permit2_approve(
            token=sell_token,
            spender=universal_router,
            amount=amount_in,
            expiration=deadline,
        ),
    )
    ur_execute = Interaction(
        target=universal_router,
        value=0,
        call_data=encode_execute(
            commands=bytes([COMMAND_V4_SWAP]),
            inputs=[
                encode_v4_swap_input(
                    sell_token=sell_token,
                    buy_token=buy_token,
                    fee=fee,
                    tick_spacing=tick_spacing,
                    amount_in=amount_in,
                    amount_out_minimum=amount_out_minimum,
                )
            ],
            deadline=deadline,
        ),
        inputs=((sell_token, amount_in),),
        outputs=((buy_token, executed_buy),),
    )
    return [erc20_approve, permit2_approve, ur_execute]
