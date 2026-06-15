import asyncio
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


# ── Best-so-far salvage at deadline (2026-06-12) ─────────────────────────────


def _solved_attempt(strategy: str, executed: int = 1000):
    from src.solver.orchestrator import AttemptRecord

    sol = Solution(
        id=12345,
        prices={"0x" + "aa" * 20: 1, "0x" + "bb" * 20: 1},
        trades=[Trade(kind="fulfillment", order_uid="0x" + "ab" * 56, executed_amount=executed)],
        interactions=[],
    )
    return AttemptRecord(
        strategy=strategy,
        status="solved",
        latency_ms=100,
        solution=sol.model_dump(mode="json"),
        error=None,
    )


def _timeout_orchestrator(records):
    """Orchestrator mock: fills attempts with completed records, then hangs
    past the solve timeout — exactly the measured 2026-06-12 failure shape."""
    import asyncio as _asyncio

    class _Orch:
        async def solve(self, auction, attempts):
            attempts.extend(records)
            await _asyncio.sleep(60)

    return _Orch()


def test_solve_timeout_salvages_completed_router_solution(
    auction_payload: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.main.settings.solve_timeout_seconds", 0.05)
    app = create_app(orchestrator=_timeout_orchestrator([_solved_attempt("router-v2")]))
    client = TestClient(app)
    resp = client.post("/solve", json=auction_payload)
    assert resp.status_code == 200
    sols = resp.json()["solutions"]
    assert len(sols) == 1
    # Wire format must be the OpenAPI alias form, same as the normal path.
    assert sols[0]["trades"][0]["order"] == "0x" + "ab" * 56
    assert "orderUid" not in sols[0]["trades"][0]
    assert "order_uid" not in sols[0]["trades"][0]


def test_solve_timeout_prefers_router_over_naive(
    auction_payload: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.main.settings.solve_timeout_seconds", 0.05)
    app = create_app(
        orchestrator=_timeout_orchestrator(
            [_solved_attempt("naive", executed=111), _solved_attempt("router-v2", executed=222)]
        )
    )
    client = TestClient(app)
    resp = client.post("/solve", json=auction_payload)
    sols = resp.json()["solutions"]
    assert len(sols) == 1
    assert sols[0]["trades"][0]["executedAmount"] == "222"


def test_solve_timeout_never_salvages_naive(
    auction_payload: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """naive fabricates phantom CIP-14 scores (oracle prices, verified live
    2026-05-24) — the orchestrator never submits it, and the salvage path
    must not reintroduce it behind the composer's back."""
    monkeypatch.setattr("src.main.settings.solve_timeout_seconds", 0.05)
    app = create_app(orchestrator=_timeout_orchestrator([_solved_attempt("naive")]))
    client = TestClient(app)
    resp = client.post("/solve", json=auction_payload)
    assert resp.json() == {"solutions": []}


def test_salvage_accepts_production_alias_record_form(
    auction_payload: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production AttemptRecords are model_dump(mode="json", by_alias=True) —
    salvage must round-trip THAT form (reviewer-requested lock)."""
    from src.solver.orchestrator import AttemptRecord

    sol = Solution(
        id=12345,
        prices={"0x" + "aa" * 20: 1, "0x" + "bb" * 20: 1},
        trades=[Trade(kind="fulfillment", order_uid="0x" + "cd" * 56, executed_amount=333)],
        interactions=[],
    )
    rec = AttemptRecord(
        strategy="router-v2",
        status="solved",
        latency_ms=100,
        solution=sol.model_dump(mode="json", by_alias=True),
        error=None,
    )
    monkeypatch.setattr("src.main.settings.solve_timeout_seconds", 0.05)
    app = create_app(orchestrator=_timeout_orchestrator([rec]))
    client = TestClient(app)
    sols = client.post("/solve", json=auction_payload).json()["solutions"]
    assert sols[0]["trades"][0]["order"] == "0x" + "cd" * 56
    assert sols[0]["trades"][0]["executedAmount"] == "333"


def test_salvage_falls_through_corrupt_candidate(
    auction_payload: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt router record must not kill the salvage — the next valid
    candidate (bipartite) ships instead."""
    from src.solver.orchestrator import AttemptRecord

    corrupt = AttemptRecord(
        strategy="router-v2", status="solved", latency_ms=1,
        solution={"trades": "garbage"}, error=None,
    )
    good = _solved_attempt("cow-matching-bipartite", executed=444)
    monkeypatch.setattr("src.main.settings.solve_timeout_seconds", 0.05)
    app = create_app(orchestrator=_timeout_orchestrator([corrupt, good]))
    client = TestClient(app)
    sols = client.post("/solve", json=auction_payload).json()["solutions"]
    assert len(sols) == 1
    assert sols[0]["trades"][0]["executedAmount"] == "444"


def test_solve_timeout_without_completed_solutions_stays_empty(
    auction_payload: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.main.settings.solve_timeout_seconds", 0.05)
    app = create_app(orchestrator=_timeout_orchestrator([]))
    client = TestClient(app)
    resp = client.post("/solve", json=auction_payload)
    assert resp.status_code == 200
    assert resp.json() == {"solutions": []}


# ── Liveness watchdog (2026-06-15) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_watchdog_restarts_when_traffic_but_no_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receiving traffic + no solve progress past the stall window → os._exit."""
    import time as _time
    from types import SimpleNamespace

    from src import main as main_mod

    monkeypatch.setattr(main_mod.settings, "watchdog_enabled", True)
    monkeypatch.setattr(main_mod.settings, "watchdog_poll_seconds", 0.01)
    monkeypatch.setattr(main_mod.settings, "watchdog_traffic_window_seconds", 100.0)
    monkeypatch.setattr(main_mod.settings, "watchdog_stall_seconds", 50.0)

    now = _time.monotonic()
    app = SimpleNamespace(
        state=SimpleNamespace(
            # Traffic 1s ago (within window), progress 999s ago (past stall).
            last_request_ts=now - 1.0,
            last_progress_ts=now - 999.0,
        )
    )
    exited: list[int] = []
    monkeypatch.setattr(main_mod.os, "_exit", lambda code: exited.append(code))

    # The watchdog seeds last_progress/last_request to `now` at startup; force
    # them stale again right after so the first poll trips.
    task = asyncio.create_task(main_mod._liveness_watchdog(app))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    app.state.last_request_ts = now - 1.0
    app.state.last_progress_ts = now - 999.0
    for _ in range(50):
        await asyncio.sleep(0.01)
        if exited:
            break
    task.cancel()
    assert exited == [1]


@pytest.mark.asyncio
async def test_watchdog_no_restart_without_traffic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No recent traffic → a stalled solver is just idle, never restarted."""
    import time as _time
    from types import SimpleNamespace

    from src import main as main_mod

    monkeypatch.setattr(main_mod.settings, "watchdog_enabled", True)
    monkeypatch.setattr(main_mod.settings, "watchdog_poll_seconds", 0.01)
    monkeypatch.setattr(main_mod.settings, "watchdog_traffic_window_seconds", 100.0)
    monkeypatch.setattr(main_mod.settings, "watchdog_stall_seconds", 50.0)

    now = _time.monotonic()
    app = SimpleNamespace(
        state=SimpleNamespace(
            last_request_ts=now - 999.0,  # no traffic for ages
            last_progress_ts=now - 999.0,
        )
    )
    exited: list[int] = []
    monkeypatch.setattr(main_mod.os, "_exit", lambda code: exited.append(code))

    task = asyncio.create_task(main_mod._liveness_watchdog(app))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    app.state.last_request_ts = now - 999.0
    app.state.last_progress_ts = now - 999.0
    for _ in range(20):
        await asyncio.sleep(0.01)
    task.cancel()
    assert exited == []


@pytest.mark.asyncio
async def test_watchdog_disabled_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from src import main as main_mod

    monkeypatch.setattr(main_mod.settings, "watchdog_enabled", False)
    exited: list[int] = []
    monkeypatch.setattr(main_mod.os, "_exit", lambda code: exited.append(code))
    app = SimpleNamespace(state=SimpleNamespace())
    await main_mod._liveness_watchdog(app)  # type: ignore[arg-type]  # returns immediately
    assert exited == []


def test_solve_progress_wiring(auction_payload: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """The watchdog's correctness rests on this: a completed solve (incl.
    NoSolution) advances last_progress_ts; a TimeoutError does NOT."""
    # 1) NoSolution → progress advances.
    orch = AsyncMock()
    orch.solve.return_value = (NoSolution(), None)
    app = create_app(orchestrator=orch)
    client = TestClient(app)
    client.post("/solve", json=auction_payload)
    ts_after_nosol = app.state.last_progress_ts
    assert ts_after_nosol is not None

    # 2) TimeoutError → progress must NOT advance past the no_solution value.
    async def hang(_a: object, _b: object) -> object:
        raise TimeoutError

    orch.solve.side_effect = hang
    monkeypatch.setattr("src.main.settings.solve_timeout_seconds", 0.05)
    client.post("/solve", json=auction_payload)
    assert app.state.last_progress_ts == ts_after_nosol  # unchanged by the timeout
