import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from src.main import create_app
from src.models.solution import Solution, Trade
from src.solver.base import NoSolution


@pytest.fixture
def auction_payload() -> dict:
    path = Path(__file__).parent / "fixtures" / "sample_auction.json"
    return json.loads(path.read_text())


def test_health_returns_ok() -> None:
    app = create_app(orchestrator=AsyncMock())
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_metrics_endpoint_serves_prometheus(auction_payload: dict) -> None:
    app = create_app(orchestrator=AsyncMock())
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert b"cow_solver_solve_total" in resp.content


def test_solve_with_solution(auction_payload: dict) -> None:
    orch = AsyncMock()
    orch.solve.return_value = (
        Solution(
            id=12345,
            prices={"0x82af49447d8a07e3bd95bd0d56f35241523fbab1": 3500 * 10**18},
            trades=[Trade(kind="fulfillment", order_uid="0x" + "a"*112, executed_amount=10**18)],
            interactions=[],
        ),
        [],  # empty attempts list
    )
    app = create_app(orchestrator=orch)
    client = TestClient(app)
    resp = client.post("/solve", json=auction_payload)
    assert resp.status_code == 200
    body = resp.json()
    # OpenAPI shape: {"solutions": [Solution]}
    assert "solutions" in body
    assert len(body["solutions"]) == 1
    solution = body["solutions"][0]
    assert solution["id"] == 12345
    assert len(solution["trades"]) == 1


def test_solve_with_no_solution(auction_payload: dict) -> None:
    orch = AsyncMock()
    orch.solve.return_value = (NoSolution(), [])  # tuple with empty attempts
    app = create_app(orchestrator=orch)
    client = TestClient(app)
    resp = client.post("/solve", json=auction_payload)
    # OpenAPI: empty solutions array signals "we don't participate" — distinct
    # from a Solution with empty trades, which would be interpreted as a
    # valid (but pointless) settlement attempt.
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"solutions": []}


def test_notify_endpoint_acknowledges() -> None:
    """The driver POSTs status notifications after each auction; we must
    accept them with 200 OK so the driver doesn't retry/queue them."""
    app = create_app(orchestrator=AsyncMock())
    client = TestClient(app)
    resp = client.post("/notify", json={
        "auctionId": "12345",
        "solutionId": 1,
        "kind": "success",
    })
    assert resp.status_code == 200
