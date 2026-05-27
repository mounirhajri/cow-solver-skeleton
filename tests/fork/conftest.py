"""Pytest scaffolding for opt-in on-chain simulation tests.

These tests use ``eth_call`` against a real Arbitrum RPC (or a fork
managed externally via Tenderly / Anvil / Foundry) to verify that
encoder output is actually executable on-chain. Without this layer,
unit tests only confirm ABI round-trip — they can't catch e.g. wrong
selector vs target-router mismatch or stale fee-tier semantics.

Activation: set ``ARBITRUM_RPC_URL`` to enable. Without it every test in
this directory is silently skipped — CI without RPC access stays green.

Typical workflow:

    # local dev (Alchemy free tier is enough for the small read volume here)
    export ARBITRUM_RPC_URL="https://arb-mainnet.g.alchemy.com/v2/<key>"
    pytest tests/fork -v

    # against an Anvil fork at a pinned block (deterministic)
    anvil --fork-url $REAL_ARBITRUM_RPC --fork-block-number 250000000
    ARBITRUM_RPC_URL=http://localhost:8545 pytest tests/fork -v

Fork-block pinning matters: V3 quoter output drifts block-to-block as
pool state moves, so assertions that compare exact wei amounts would
flake against live mainnet. Either pin a block in the env or write the
test to assert non-revert + monotonic properties rather than exact wei.
"""

from __future__ import annotations

import os

import pytest


def _fork_rpc_url() -> str | None:
    """Return the RPC URL to drive on-chain simulations against, or None."""
    url = os.environ.get("ARBITRUM_RPC_URL", "").strip()
    return url or None


@pytest.fixture(scope="session")
def arbitrum_rpc_url() -> str:
    """Skip the test if ``ARBITRUM_RPC_URL`` is unset.

    Session-scoped so the skip is decided once per session — paying the
    env-lookup cost per-test would be needless.
    """
    url = _fork_rpc_url()
    if url is None:
        pytest.skip(
            "ARBITRUM_RPC_URL not set — fork tests skipped. "
            "Set it to any Arbitrum RPC (Alchemy free tier or local Anvil) "
            "to enable on-chain simulation."
        )
    return url
