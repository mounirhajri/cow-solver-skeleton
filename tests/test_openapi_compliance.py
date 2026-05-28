"""OpenAPI compliance tests for the /solve endpoint.

Verifies that the request shape sent by the CoW driver and the response
shape we return both conform to the solver-engine OpenAPI spec at
``docs/cow-spec/solver-engine-openapi.yml``. These tests guard against
silent drift between our handler and the contract the driver enforces.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from src.main import create_app
from src.models.solution import Solution, Trade
from src.solver.base import NoSolution


def _driver_shaped_auction() -> dict:
    """Minimal Auction payload as the CoW driver would POST it.

    Includes every field marked ``required`` in the OpenAPI Auction schema
    plus nullable ``id`` to exercise the quote-request path. Token entries
    use string-encoded amounts because the spec's TokenAmount type is U256.
    """
    return {
        "id": "auction-1",
        "tokens": {
            "0xaf88d065e77c8cc2239327c5edb3a432268e5831": {
                "decimals": 6,
                "symbol": "USDC",
                "referencePrice": "1000000000000000000",
                "availableBalance": "100000000000",
                "trusted": True,
            },
            "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": {
                "decimals": 6,
                "symbol": "USDT",
                "referencePrice": "1000000000000000000",
                "availableBalance": "100000000000",
                "trusted": True,
            },
        },
        "orders": [],
        "liquidity": [],
        "effectiveGasPrice": "1500000000",
        "deadline": "2026-06-01T12:00:00Z",
        "surplusCapturingJitOrderOwners": [],
    }


def test_solve_accepts_full_driver_request_shape() -> None:
    """Request with all OpenAPI-required Auction fields must parse + solve."""
    orch = AsyncMock()
    orch.solve.return_value = (NoSolution(), [])
    app = create_app(orchestrator=orch)
    client = TestClient(app)

    resp = client.post("/solve", json=_driver_shaped_auction())
    assert resp.status_code == 200
    assert resp.json() == {"solutions": []}


def test_solve_response_matches_openapi_shape() -> None:
    """Successful solve returns ``{"solutions": [Solution]}`` per spec.

    The Solution object must have id (number), prices (map[address->U256]),
    trades (array), and interactions (array). Missing required keys would
    cause the driver to reject the response.
    """
    weth = "0x82af49447d8a07e3bd95bd0d56f35241523fbab1"
    usdc = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
    orch = AsyncMock()
    orch.solve.return_value = (
        Solution(
            id=42,
            prices={weth: 3500 * 10**18, usdc: 10**18},
            trades=[
                Trade(
                    kind="fulfillment",
                    order_uid="0x" + "a" * 112,
                    executed_amount=10**18,
                ),
            ],
            interactions=[],
        ),
        [],
    )
    app = create_app(orchestrator=orch)
    client = TestClient(app)

    resp = client.post("/solve", json=_driver_shaped_auction())
    assert resp.status_code == 200
    body = resp.json()

    assert "solutions" in body
    assert isinstance(body["solutions"], list)
    assert len(body["solutions"]) == 1

    sol = body["solutions"][0]
    # Required Solution fields per OpenAPI
    assert sol["id"] == 42
    assert isinstance(sol["prices"], dict)
    # U256 must be a string for big-number safety
    assert all(isinstance(v, str) for v in sol["prices"].values())
    assert isinstance(sol["trades"], list)
    assert isinstance(sol["interactions"], list)
    # Trade.executedAmount must also be U256-string
    assert isinstance(sol["trades"][0]["executedAmount"], str)


def test_solve_accepts_nullable_id() -> None:
    """Spec allows ``id: null`` for non-auction (quote) requests. The driver
    uses this when asking the solver to price a token without running a
    real auction. We must not 422 on null id, and we must not call into
    the orchestrator (which would crash on ``int(None)`` in legacy paths
    that assume an auction is in progress)."""
    payload = _driver_shaped_auction()
    payload["id"] = None
    orch = AsyncMock()
    # If the orchestrator IS called, the spy will record it — quote-mode
    # must short-circuit before reaching the solver chain.
    orch.solve.return_value = (NoSolution(), [])
    app = create_app(orchestrator=orch)
    client = TestClient(app)

    resp = client.post("/solve", json=payload)
    assert resp.status_code == 200
    assert resp.json() == {"solutions": []}
    # Quote-only requests must NOT trigger a solve attempt — the
    # downstream persist + naive code paths assume a non-None auction.id
    # and the spec semantics of quote-mode is "price tokens, don't solve".
    orch.solve.assert_not_called()


def test_solve_ignores_unknown_driver_fields() -> None:
    """Newer driver versions may add fields the spec doesn't carry yet.
    We accept and ignore them rather than 422'ing on schema drift."""
    payload = _driver_shaped_auction()
    payload["futureField"] = {"some": "value"}
    payload["anotherField"] = [1, 2, 3]
    orch = AsyncMock()
    orch.solve.return_value = (NoSolution(), [])
    app = create_app(orchestrator=orch)
    client = TestClient(app)

    resp = client.post("/solve", json=payload)
    assert resp.status_code == 200


def test_notify_accepts_driver_status_callbacks() -> None:
    """Driver POSTs a notification per auction with the outcome of our
    solution. We acknowledge with 200 — non-200 would make the driver
    queue retries which is wasted load on both sides."""
    app = create_app(orchestrator=AsyncMock())
    client = TestClient(app)

    for kind in ("success", "revert", "simulationFailed", "timeout"):
        resp = client.post(
            "/notify",
            json={"auctionId": "auction-1", "solutionId": 1, "kind": kind},
        )
        assert resp.status_code == 200, f"notify {kind} should ack with 200"


def test_openapi_spec_is_checked_in() -> None:
    """Spec file lives in the repo so this contract is reviewable in PRs.
    Bumping the CoW driver version means re-downloading the spec and
    re-running this suite."""
    spec_path = (
        Path(__file__).parent.parent
        / "docs"
        / "cow-spec"
        / "solver-engine-openapi.yml"
    )
    assert spec_path.exists(), (
        f"OpenAPI spec missing at {spec_path}. Refresh with:\n"
        "  curl -s https://raw.githubusercontent.com/cowprotocol/services/"
        "main/crates/solvers/openapi.yml -o docs/cow-spec/solver-engine-openapi.yml"
    )
    text = spec_path.read_text()
    # Sanity: file is actually the solver-engine spec we expect
    assert "/solve" in text
    assert "Auction:" in text
    assert "Solution:" in text
