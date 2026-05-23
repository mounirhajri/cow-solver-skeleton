"""Unit tests for the RF token-quality pre-filter."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from edge.matching.rf_filter import (
    _fetch_token_features,
    filter_orders_by_token_quality,
)
from src.models.order import Order


def _mk_order(uid: str, sell_token: str, buy_token: str) -> Order:
    return Order(
        uid=uid,
        sellToken=sell_token,
        buyToken=buy_token,
        sellAmount=1000,
        buyAmount=800,
        feePolicies=[],
        validTo=999999,
        kind="sell",
        owner="0x" + "a" * 40,
        partiallyFillable=False,
        **{"class": "limit"},
    )


@dataclass
class _FakeClassifier:
    """Test double for TokenClassifier.

    `scores_by_token` is a dict mapping lower-case token address → P(legit).
    Tokens absent from the dict fall back to NEUTRAL_SCORE-ish (`default`).
    `model` is a sentinel so the filter does not short-circuit.
    """

    scores_by_token: dict[str, float]
    model: Any = "loaded"
    default: float = 0.5

    def score(self, features: dict) -> float:  # noqa: ARG002
        # `features` would normally drive the score; tests stub the score
        # per token via a wrapper that mutates state — see tests below.
        return self.default


@pytest.fixture
async def feature_session_factory():
    """In-memory sqlite with a token_features table."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE token_features (
                    token_address TEXT PRIMARY KEY,
                    decimals INTEGER,
                    contract_verified INTEGER,
                    has_transfer_tax INTEGER,
                    bridge_canonical INTEGER,
                    tvl_usd NUMERIC,
                    volume_24h_usd NUMERIC,
                    pool_count_v2 INTEGER,
                    pool_count_v3 INTEGER,
                    pool_count_camelot INTEGER,
                    holder_count INTEGER,
                    top10_concentration NUMERIC,
                    age_blocks INTEGER,
                    on_arbitrum_token_list INTEGER,
                    on_coingecko INTEGER,
                    last_refreshed TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


# ── No-op fallbacks ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_noop_when_classifier_none():
    orders = [_mk_order("o1", "0xa", "0xb")]
    out = await filter_orders_by_token_quality(orders, MagicMock(), None)
    assert out == orders


@pytest.mark.asyncio
async def test_noop_when_model_none():
    orders = [_mk_order("o1", "0xa", "0xb")]
    cls = _FakeClassifier(scores_by_token={}, model=None)
    out = await filter_orders_by_token_quality(orders, MagicMock(), cls)
    assert out == orders


@pytest.mark.asyncio
async def test_noop_when_session_factory_none():
    orders = [_mk_order("o1", "0xa", "0xb")]
    cls = _FakeClassifier(scores_by_token={})
    out = await filter_orders_by_token_quality(orders, None, cls)
    assert out == orders


@pytest.mark.asyncio
async def test_empty_orders_returns_empty():
    cls = _FakeClassifier(scores_by_token={})
    out = await filter_orders_by_token_quality([], MagicMock(), cls)
    assert out == []


# ── Filter behaviour ─────────────────────────────────────────────────────────


class _PerTokenClassifier:
    """Scores by reading `_current_token` set externally — but we don't have
    that hook in score(); easier: capture token from features dict.

    Instead, we set scores via the features dict using a sentinel key.
    """

    model = "loaded"

    def __init__(self, scores: dict[str, float]) -> None:
        # scores keyed by lower-case address
        self.scores = scores

    def score(self, features: dict) -> float:
        # Test wires the address into features via the conftest setup below.
        addr = features.get("__test_addr__")
        if addr is None:
            return 0.5
        return self.scores.get(addr.lower(), 0.5)


@pytest.mark.asyncio
async def test_filters_out_low_sell_token(feature_session_factory, monkeypatch):
    """Order whose sell_token scores < threshold is dropped."""
    cls = _PerTokenClassifier(scores={"0xa": 0.1, "0xb": 0.9})

    # Patch _fetch_token_features to inject the address into the feature dict
    # so _PerTokenClassifier can route per-token.
    async def fake_fetch(_factory, addrs):
        return {a.lower(): {"__test_addr__": a.lower()} for a in addrs}

    monkeypatch.setattr("edge.matching.rf_filter._fetch_token_features", fake_fetch)

    orders = [
        _mk_order("o1", "0xa", "0xb"),  # sell=0.1 → drop
        _mk_order("o2", "0xb", "0xa"),  # buy=0.1  → drop
    ]
    out = await filter_orders_by_token_quality(orders, feature_session_factory, cls)
    assert out == []


@pytest.mark.asyncio
async def test_filters_out_low_buy_token(feature_session_factory, monkeypatch):
    cls = _PerTokenClassifier(scores={"0xa": 0.9, "0xb": 0.2})

    async def fake_fetch(_factory, addrs):
        return {a.lower(): {"__test_addr__": a.lower()} for a in addrs}

    monkeypatch.setattr("edge.matching.rf_filter._fetch_token_features", fake_fetch)

    orders = [_mk_order("o1", "0xa", "0xb")]
    out = await filter_orders_by_token_quality(orders, feature_session_factory, cls)
    assert out == []


@pytest.mark.asyncio
async def test_keeps_orders_when_both_pass(feature_session_factory, monkeypatch):
    cls = _PerTokenClassifier(scores={"0xa": 0.9, "0xb": 0.7})

    async def fake_fetch(_factory, addrs):
        return {a.lower(): {"__test_addr__": a.lower()} for a in addrs}

    monkeypatch.setattr("edge.matching.rf_filter._fetch_token_features", fake_fetch)

    orders = [_mk_order("o1", "0xa", "0xb"), _mk_order("o2", "0xb", "0xa")]
    out = await filter_orders_by_token_quality(orders, feature_session_factory, cls)
    assert len(out) == 2


@pytest.mark.asyncio
async def test_unknown_token_passes_via_neutral_score(feature_session_factory):
    """Token with no features row → classifier.score({}) returns 0.5 → passes 0.4."""
    cls = _FakeClassifier(scores_by_token={}, default=0.5)
    orders = [_mk_order("o1", "0xa", "0xb")]
    # The DB has zero rows in token_features → both tokens unknown
    out = await filter_orders_by_token_quality(orders, feature_session_factory, cls)
    assert len(out) == 1


@pytest.mark.asyncio
async def test_logs_event(feature_session_factory, monkeypatch):
    cls = _PerTokenClassifier(scores={"0xa": 0.9, "0xb": 0.7})

    async def fake_fetch(_factory, addrs):
        return {a.lower(): {"__test_addr__": a.lower()} for a in addrs}

    monkeypatch.setattr("edge.matching.rf_filter._fetch_token_features", fake_fetch)

    events: list[dict] = []

    def capture(event, **kw):
        events.append({"event": event, **kw})

    monkeypatch.setattr("edge.matching.rf_filter.log.info", capture)

    orders = [_mk_order("o1", "0xa", "0xb"), _mk_order("o2", "0xb", "0xa")]
    await filter_orders_by_token_quality(orders, feature_session_factory, cls, threshold=0.4)

    applied = [e for e in events if e["event"] == "rf_filter_applied"]
    assert len(applied) == 1
    assert applied[0]["n_in"] == 2
    assert applied[0]["n_out"] == 2
    assert applied[0]["n_unique_tokens"] == 2
    assert applied[0]["threshold"] == 0.4


# ── _fetch_token_features round-trip ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_token_features_lowercases(feature_session_factory):
    async with feature_session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO token_features (token_address, decimals) VALUES (:a, :d)"
            ),
            {"a": "0xabc", "d": 18},
        )
        await s.commit()

    # Query with mixed-case address — should match the lower-cased row
    out = await _fetch_token_features(feature_session_factory, ["0xABC"])
    assert "0xabc" in out
    assert out["0xabc"]["decimals"] == 18


@pytest.mark.asyncio
async def test_fetch_token_features_empty_input(feature_session_factory):
    out = await _fetch_token_features(feature_session_factory, [])
    assert out == {}
