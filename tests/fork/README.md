# Fork tests — on-chain calldata simulation

These tests exercise encoder output against a real Arbitrum RPC via
`eth_call`. They catch failure modes that ABI round-trip tests cannot:
wrong router targeting, missing fee-tier liquidity, transfer-hook
incompatibility with the Settlement contract, and similar production
hazards.

## Activation

Tests in this directory **skip by default**. Set `ARBITRUM_RPC_URL` to
enable:

```bash
export ARBITRUM_RPC_URL="https://arb-mainnet.g.alchemy.com/v2/<key>"
pytest tests/fork -v
```

Alchemy's free tier handles the read volume here. The whole suite runs
single-digit `eth_call`s.

## Deterministic runs against an Anvil fork

Live mainnet drifts block-to-block. If you want stable assertions that
compare exact wei amounts (e.g. for regression suite), fork at a pinned
block:

```bash
# Foundry must be installed: https://book.getfoundry.sh/getting-started/installation
anvil --fork-url $REAL_ARBITRUM_RPC --fork-block-number 250000000 &
ARBITRUM_RPC_URL=http://localhost:8545 pytest tests/fork -v
```

The conftest doesn't care which RPC URL — it just needs to be Arbitrum.

## What's covered today

| Test | What it checks |
|------|----------------|
| `test_exact_input_single_calldata_executes_against_v3_router` | Our encoded calldata reaches the right function on the right contract — selector mismatch would be caught here, not at solver-submission time |

## What's NOT covered (future work)

These need a writable fork (Anvil/Tenderly) and a more elaborate setup:

- **End-to-end settlement**: encode → call `GPv2Settlement.settle(...)` →
  check user balance changes match the claimed clearing prices.
- **Allowance preflight**: simulate `approve` from Settlement to the V3
  router and verify subsequent swap doesn't revert.
- **USDT edge case**: token requires `approve(0)` before re-approving
  non-zero — a real fork exercises this without polluting prod.
- **Fee-on-transfer detection**: confirm tokens flagged by
  `TokenFeatures.has_transfer_tax` are correctly skipped and don't reach
  the encoder.

When you're ready to add these, add `tests/fork/test_settle_*.py` and
extend the conftest with a `fork_client` fixture that wraps web3.py or
a similar lib pointing at the writable fork RPC.
