"""The global RPC concurrency gate bounds total in-flight eth_calls to the
shared node across ALL RpcClient instances, and releases the slot during the
retry backoff so a parked caller doesn't idle it. See src/routing/rpc.py."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from src.routing import rpc as rpc_mod
from src.routing.rpc import RpcClient


@pytest.fixture(autouse=True)
def _fresh_gate():
    """The per-loop gate caches the semaphore at its FIRST creation, capturing
    settings.rpc_max_concurrent then. Clear the cache around each test so a
    monkeypatched N takes effect and no stale-N gate leaks between tests."""
    rpc_mod._rpc_semaphores.clear()
    yield
    rpc_mod._rpc_semaphores.clear()


def _result_resp() -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.json = lambda: {"jsonrpc": "2.0", "id": 1, "result": "0x01"}
    return r


def _resp_429() -> MagicMock:
    r = MagicMock()
    r.status_code = 429
    r.json = lambda: {}
    return r


@pytest.mark.asyncio
async def test_gate_caps_in_flight_across_instances_and_both_entry_points(monkeypatch) -> None:
    """20 concurrent calls across TWO RpcClient instances and BOTH eth_call /
    eth_call_capture entry points must never exceed rpc_max_concurrent in
    flight — proving the gate is process-global, not per-instance/per-method."""
    monkeypatch.setattr(rpc_mod.settings, "rpc_max_concurrent", 2)

    state = {"cur": 0, "max": 0}

    async def fake_post(url, json=None, timeout=None):  # noqa: ANN001
        state["cur"] += 1
        state["max"] = max(state["max"], state["cur"])
        await asyncio.sleep(0.01)  # hold so concurrency can pile up
        state["cur"] -= 1
        return _result_resp()

    def make_client(*_a, **_k):
        c = MagicMock()
        c.post = fake_post
        return c

    with patch("src.routing.rpc.Web3", return_value=MagicMock()), \
         patch("src.routing.rpc.httpx.AsyncClient", side_effect=make_client):
        c1 = RpcClient("https://rpc.example")
        c2 = RpcClient("https://rpc.example")
        tasks = (
            [c1.eth_call("0xabc", "0x12") for _ in range(10)]
            + [c2.eth_call_capture("0xabc", "0x12") for _ in range(10)]
        )
        await asyncio.gather(*tasks)

    assert state["max"] <= 2, f"in-flight {state['max']} exceeded the cap of 2"
    assert state["max"] == 2, "gate over-serialised — never reached the allowed 2"


@pytest.mark.asyncio
async def test_backoff_releases_the_slot(monkeypatch) -> None:
    """With N=1, while caller A is parked in its retry backoff, caller B must be
    able to acquire the slot — i.e. B's request lands BEFORE A's retry request."""
    monkeypatch.setattr(rpc_mod.settings, "rpc_max_concurrent", 1)

    events: list[str] = []
    a_n = {"n": 0}

    async def fake_post(url, json=None, timeout=None):  # noqa: ANN001
        to = json["params"][0]["to"]
        if to == "0xaaa":
            a_n["n"] += 1
            events.append(f"A{a_n['n']}")
            return _resp_429() if a_n["n"] == 1 else _result_resp()
        events.append("B")
        return _result_resp()

    def make_client(*_a, **_k):
        c = MagicMock()
        c.post = fake_post
        return c

    with patch("src.routing.rpc.Web3", return_value=MagicMock()), \
         patch("src.routing.rpc.httpx.AsyncClient", side_effect=make_client):
        c = RpcClient("https://rpc.example")
        a = asyncio.create_task(c.eth_call("0xaaa", "0x12"))
        await asyncio.sleep(0.01)  # let A post (429) and enter its 0.2s backoff
        b = asyncio.create_task(c.eth_call("0xbbb", "0x12"))
        await asyncio.gather(a, b)

    # If the slot were held during backoff, order would be A1, A2, B.
    assert events.index("B") < events.index("A2"), f"slot not released: {events}"
