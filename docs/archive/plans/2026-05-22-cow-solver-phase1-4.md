# CoW Solver Implementation Plan: Phase 1 – 4

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take the deployed `cow-solver` from its current degraded shadow-only naive state to first on-chain Barn settlements on Arbitrum, with a competitive multi-strategy architecture targeting ≥5% hypothetical win-rate before KYC investment.

**Architecture (revised vs original spec):**

Router-primary edge instead of CoW-matching-primary. Empirical surplus distribution on Arbitrum is ~70% routing, ~15% CoW-matching, ~15% JIT/RFQ. The original spec optimized for the smaller slice. Updated pyramid:

```
[1] Multi-hop AMM router (UniV2 → UniV3 → Camelot/Algebra)   ← workhorse
[2] CoW-matching layer (Johnson rings, on top of router)     ← bonus when present
[3] LightGBM pool/token quality filter                       ← survival feature
[4] JIT liquidity                                            ← Phase 4+ only
[5] 1inch baseline                                           ← sanity check, not fallback
```

**Tech Stack:**
- Python 3.12, uv, FastAPI, Pydantic v2, httpx, structlog
- rustworkx (cycle enumeration), scipy (LP)
- LightGBM (classifier), scikit-learn (cold-start RF)
- PostgreSQL (shadow data, features, labels), Redis (pool cache, feature cache)
- web3.py + Multicall3 (RPC efficiency)
- Docker Compose, GitHub Actions → GHCR → Hetzner

**Spec reference:** [docs/superpowers/specs/2026-05-22-cow-solver-design.md](../specs/2026-05-22-cow-solver-design.md)
**Predecessor plan:** [2026-05-22-phase0-1-skeleton-shadow.md](2026-05-22-phase0-1-skeleton-shadow.md) — partially executed; this plan supersedes its Phase 1 architecture decisions.

---

## Current State Snapshot (2026-05-22, 22:00 UTC)

Server `116.203.81.107` (Hetzner CX22, shared with AI Backoffice):

| Resource | State | Notes |
|---|---|---|
| Disk | 32G/38G (90%) | post-cleanup; still tight |
| RAM | 127 MB free / 3.7 GB total, 1.1 GB swap active | very tight |
| `cow-solver` | healthy | running naive only — `edge_strategies_not_present` |
| `cow-reconciler` | unhealthy | healthcheck inherited from solver (wrong) |
| `cow-shadow-poller` | unhealthy | same healthcheck issue + rate-limit (429) on CoW API |
| `backoffice-api/worker` (prod) | removed | replaced by staging during shadow phase |
| Edge submodule (matching) | committed, not deployed | Johnson skeleton in [edge/matching/multi_party.py](../../edge/matching/multi_party.py) |
| Edge submodule (pool_indexer, classifier) | empty | blocks edge loading in orchestrator |

This plan starts with a 1-day Phase 0' that addresses the three concrete blockers, then proceeds to Phase 1.

---

## File Structure (additions over current repo)

```
cow-solver-skeleton/
├── src/
│   ├── persistence/                    # NEW — Postgres layer
│   │   ├── __init__.py
│   │   ├── db.py                       # async engine + session factory
│   │   ├── models.py                   # SQLAlchemy ORM models
│   │   └── migrations/
│   │       ├── env.py
│   │       └── versions/
│   ├── routing/
│   │   ├── amm_v2.py                   # NEW — UniV2 / Camelot V2 router
│   │   ├── amm_v3.py                   # NEW — UniV3 / Algebra router (Phase 2)
│   │   ├── multihop.py                 # NEW — DFS multi-hop search
│   │   └── multicall.py                # NEW — batched eth_call helper
│   └── shadow/
│       └── persist.py                  # NEW — write shadow batches to Postgres
├── edge/
│   ├── matching/
│   │   ├── bipartite.py                # NEW — 2-party CoW matcher
│   │   ├── surplus.py                  # NEW — LP for ring volume distribution
│   │   └── composer.py                 # NEW — CIP-67 multi-ring composition
│   ├── pool_indexer/
│   │   ├── factory_watcher.py          # NEW — WebSocket on UniV2/V3/Camelot factories
│   │   ├── pool_cache.py               # NEW — Redis-backed pool state cache
│   │   └── liquidity_scorer.py         # NEW — feature extraction for classifier
│   └── classifier/
│       ├── data_collector.py           # NEW — auction outcomes → Postgres
│       ├── feature_extractor.py        # NEW — async enrichment worker
│       ├── label_generator.py          # NEW — derive labels from outcomes
│       ├── train.py                    # NEW — weekly training entrypoint
│       ├── evaluate.py                 # NEW — new vs prod model
│       ├── model_registry.py           # NEW — versioned pickle storage
│       └── predict.py                  # NEW — runtime inference interface
├── scripts/
│   ├── shadow_poller.py                # MODIFY — add backoff + interval tuning
│   └── reconcile_loop.py               # MODIFY — proper healthcheck endpoint
├── deploy/hetzner/
│   ├── cow-solver/
│   │   └── docker-compose.yml          # MODIFY — fix healthchecks, add trainer
│   └── prometheus/
│       └── solver-rules.yml            # NEW — alerting rules
└── tests/
    ├── test_persistence/
    ├── test_routing/
    │   ├── test_amm_v2.py
    │   ├── test_amm_v3.py
    │   └── test_multihop.py
    ├── test_edge/
    │   ├── test_bipartite.py
    │   ├── test_surplus.py
    │   ├── test_composer.py
    │   ├── test_pool_indexer.py
    │   └── test_classifier.py
    └── fixtures/
        ├── arbitrum_auction_*.json     # real shadow-captured batches
        └── arbitrum_pool_state_*.json
```

---

## Phase 0': Server Stabilization (1 day)

Goal: shadow data flows into Postgres, all containers healthy, edge submodule loadable.

### Task 0.1: Fix reconciler healthcheck

**Files:**
- Modify: `scripts/reconcile_loop.py`
- Modify: `deploy/hetzner/cow-solver/docker-compose.yml` (server-side, via push-to-deploy)

The reconciler inherits the solver's HTTP healthcheck (`urllib.urlopen('http://localhost:8000/health')`), but has no HTTP server. It runs a worker loop. Healthcheck must be a file-stat or a process-status probe.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scripts/test_reconcile_healthcheck.py
import time
from pathlib import Path
from scripts.reconcile_loop import touch_liveness, is_alive

def test_touch_liveness_creates_file(tmp_path):
    f = tmp_path / "alive"
    touch_liveness(f)
    assert f.exists()

def test_is_alive_within_window(tmp_path):
    f = tmp_path / "alive"
    touch_liveness(f)
    assert is_alive(f, max_age_seconds=120)

def test_is_alive_stale(tmp_path):
    f = tmp_path / "alive"
    f.write_text("")
    old = time.time() - 600
    import os
    os.utime(f, (old, old))
    assert not is_alive(f, max_age_seconds=120)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_scripts/test_reconcile_healthcheck.py -v
```
Expected: ImportError (functions not defined).

- [ ] **Step 3: Add liveness functions to `scripts/reconcile_loop.py`**

```python
from pathlib import Path
import time

LIVENESS_FILE = Path("/data/reconciler.alive")

def touch_liveness(path: Path = LIVENESS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()

def is_alive(path: Path = LIVENESS_FILE, max_age_seconds: int = 120) -> bool:
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) < max_age_seconds
```

Call `touch_liveness()` at the end of each `reconcile_cycle` iteration.

- [ ] **Step 4: Verify tests pass**

```bash
uv run pytest tests/test_scripts/test_reconcile_healthcheck.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Update docker-compose.yml healthcheck for reconciler**

```yaml
reconciler:
  # ... existing config ...
  healthcheck:
    test: ["CMD", "python", "-c", "from scripts.reconcile_loop import is_alive; import sys; sys.exit(0 if is_alive() else 1)"]
    interval: 30s
    timeout: 5s
    retries: 3
    start_period: 60s
```

Same change for `shadow-poller`, pointing at its own liveness file.

- [ ] **Step 6: Commit**

```bash
git add scripts/reconcile_loop.py deploy/hetzner/cow-solver/docker-compose.yml tests/test_scripts/
git commit -m "fix(ops): file-based healthcheck for reconciler and shadow-poller"
```

After push, GitHub Actions deploys. Verify on server:
```bash
ssh mounir@116.203.81.107 'docker ps --filter name=cow- --format "{{.Names}}: {{.Status}}"'
```
Expected: all three `cow-*` containers healthy within 2 minutes.

---

### Task 0.2: Shadow poller backoff + auction filtering

**Files:**
- Modify: `scripts/shadow_poller.py`
- Test: `tests/test_scripts/test_shadow_poller.py`

The poller hits two CoW endpoints in tight loops with no backoff. Root cause of 429: `/orders/{uid}` is called once per order in every polled auction (~40 calls × every 60s). The right approach: poll competition endpoint, but for order details use a single bulk query, and exponential-back-off on 429.

- [ ] **Step 1: Write the failing test for backoff state**

```python
# tests/test_scripts/test_shadow_poller.py
import pytest
from scripts.shadow_poller import Backoff

def test_backoff_starts_at_base():
    b = Backoff(base=60.0)
    assert b.current() == 60.0

def test_backoff_doubles_on_429():
    b = Backoff(base=60.0, cap=600.0)
    b.on_rate_limit()
    assert b.current() == 120.0
    b.on_rate_limit()
    assert b.current() == 240.0

def test_backoff_caps():
    b = Backoff(base=60.0, cap=300.0)
    for _ in range(10):
        b.on_rate_limit()
    assert b.current() == 300.0

def test_backoff_resets_on_success():
    b = Backoff(base=60.0)
    b.on_rate_limit(); b.on_rate_limit()
    b.on_success()
    assert b.current() == 60.0
```

- [ ] **Step 2: Run, verify fail**

```bash
uv run pytest tests/test_scripts/test_shadow_poller.py -v
```

- [ ] **Step 3: Implement Backoff**

In `scripts/shadow_poller.py`:

```python
import random
from dataclasses import dataclass, field

@dataclass
class Backoff:
    base: float = 60.0
    cap: float = 600.0
    _level: int = field(default=0)

    def current(self) -> float:
        delay = self.base * (2 ** self._level)
        # ±20% jitter to avoid thundering herd
        jitter = random.uniform(0.8, 1.2)
        return min(delay * jitter, self.cap)

    def on_rate_limit(self) -> None:
        self._level += 1

    def on_success(self) -> None:
        self._level = 0
```

- [ ] **Step 4: Pass tests**

```bash
uv run pytest tests/test_scripts/test_shadow_poller.py -v
```

- [ ] **Step 5: Wire Backoff into main loop**

Replace the fixed `await asyncio.sleep(POLL_INTERVAL)` with:

```python
backoff = Backoff(base=60.0, cap=600.0)

while True:
    try:
        result = await poll_once(solver, seen)
        if result == "rate_limited":
            backoff.on_rate_limit()
            log.warning("backoff_extended", current=backoff.current())
        else:
            backoff.on_success()
    except Exception:
        log.exception("poll_error")
        backoff.on_rate_limit()
    await asyncio.sleep(backoff.current())
```

And in `poll_once`, return `"rate_limited"` instead of swallowing the 429.

- [ ] **Step 6: Skip auctions where order count > MAX_ORDERS without fetching**

Currently it fetches `min(len(uids), 40)` orders. On large auctions this still burns 40 calls. Change: if a competition response has >40 orders, skip the auction entirely. Real solvers see all orders; shadow analysis is fine on smaller batches.

```python
if len(uids) > MAX_ORDERS:
    log.info("skip_large_auction", auction_id=auction_id, n_orders=len(uids))
    return "skipped"
```

- [ ] **Step 7: Add liveness touch**

```python
from pathlib import Path
LIVENESS = Path("/data/shadow_poller.alive")
# at end of poll_once and main loop tick:
LIVENESS.parent.mkdir(parents=True, exist_ok=True)
LIVENESS.touch()
```

- [ ] **Step 8: Commit and deploy**

```bash
git add scripts/shadow_poller.py tests/test_scripts/test_shadow_poller.py
git commit -m "fix(shadow-poller): exponential backoff, large-auction skip, liveness"
git push
```

Watch on server:
```bash
ssh mounir@116.203.81.107 'docker logs --tail 50 cow-shadow-poller | grep -E "auction_processed|backoff|skip"'
```
Expected: `auction_processed` events appearing without sustained `rate_limited_skipping_poll`.

---

### Task 0.3: Stub pool_indexer so edge loads

The orchestrator gates edge loading on **both** `edge.matching` and `edge.pool_indexer` importing cleanly. `pool_indexer` is empty, blocking the Johnson matcher we already committed.

**Files:**
- Modify: `edge/pool_indexer/__init__.py` (in the private submodule)
- Modify: `src/solver/orchestrator.py` (decouple imports)

- [ ] **Step 1: Stub LongTailRouter with NoSolution behavior**

```python
# edge/pool_indexer/__init__.py
from edge.pool_indexer.stub import LongTailRouter

__all__ = ["LongTailRouter"]
```

```python
# edge/pool_indexer/stub.py
from src.models.auction import Auction
from src.models.solution import Solution
from src.solver.base import NoSolution


class LongTailRouter:
    """Placeholder until Phase 2 pool indexer is built.

    Returns NoSolution unconditionally — allows the orchestrator to load the
    edge module so CoWMatchingSolver becomes active, while keeping the real
    long-tail routing as a Phase 2 deliverable.
    """

    name = "long-tail-stub"

    async def solve(self, auction: Auction) -> Solution | NoSolution:
        return NoSolution()
```

- [ ] **Step 2: Verify orchestrator loads both edge strategies**

```bash
uv run python -c "
from src.solver.orchestrator import load_default_strategies
print([s.name for s in load_default_strategies()])
"
```
Expected: `['cow-matching-multi-party', 'long-tail-stub', 'naive']`

- [ ] **Step 3: Commit in edge submodule and bump pointer**

```bash
cd edge && git add pool_indexer/ && git commit -m "feat(pool_indexer): stub LongTailRouter to unblock orchestrator" && cd ..
git add edge && git commit -m "chore: bump edge submodule with pool_indexer stub"
git push
```

After GHA deploy, verify on server:
```bash
ssh mounir@116.203.81.107 'docker logs --tail 50 cow-solver | grep -E "edge_strategies"'
```
Expected: `edge_strategies_loaded` instead of `edge_strategies_not_present`.

---

### Task 0.4: Exit gate

- [ ] All three `cow-*` containers healthy for 30+ minutes
- [ ] `data/shadow.jsonl` accumulating ≥1 line/minute (auctions processed)
- [ ] Solver log shows `edge_strategies_loaded` and `strategy_won strategy=naive` events
- [ ] Disk usage <85%

---

## Phase 1: Shadow Naive + Data Pipeline (2-3 weeks, ~25-40 h)

Goal: 500+ shadow batches captured in Postgres with structured outcomes, naive solver responding to every batch in <15s, analyzer reports per-batch surplus delta vs winner, ready for Phase 2 edge work.

### Task 1.1: Postgres schema + connection

**Files:**
- Create: `src/persistence/__init__.py`
- Create: `src/persistence/db.py`
- Create: `src/persistence/models.py`
- Create: `src/persistence/migrations/env.py`
- Create: `src/persistence/migrations/versions/001_initial.py`
- Modify: `pyproject.toml` (add sqlalchemy, asyncpg, alembic)

Postgres tables needed up-front:

```sql
-- auctions: every shadow-polled batch
CREATE TABLE shadow_auctions (
    auction_id BIGINT PRIMARY KEY,
    chain VARCHAR(20) NOT NULL DEFAULT 'arbitrum_one',
    polled_at TIMESTAMPTZ NOT NULL,
    deadline TIMESTAMPTZ,
    n_orders INTEGER NOT NULL,
    raw_competition JSONB NOT NULL,   -- the /solver_competition response
    raw_auction JSONB NOT NULL        -- the /auctions/{id} response (orders + tokens)
);
CREATE INDEX ix_shadow_auctions_polled_at ON shadow_auctions(polled_at);

-- our solutions (one per attempted strategy)
CREATE TABLE shadow_solutions (
    id BIGSERIAL PRIMARY KEY,
    auction_id BIGINT NOT NULL REFERENCES shadow_auctions(auction_id),
    strategy VARCHAR(50) NOT NULL,    -- 'naive', 'cow-matching-multi-party', 'long-tail-stub'
    status VARCHAR(20) NOT NULL,      -- 'solved', 'no_solution', 'error', 'timeout'
    latency_ms INTEGER,
    solution JSONB,                   -- null if not solved
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_shadow_solutions_auction ON shadow_solutions(auction_id);

-- winner solutions (from CoW competition API, for comparison)
CREATE TABLE shadow_winners (
    auction_id BIGINT PRIMARY KEY REFERENCES shadow_auctions(auction_id),
    winner_solver TEXT NOT NULL,
    score NUMERIC(40, 0),             -- wei-scale surplus
    raw_solution JSONB NOT NULL
);

-- per-token outcomes for classifier training (Phase 2)
CREATE TABLE token_outcomes (
    id BIGSERIAL PRIMARY KEY,
    token_address VARCHAR(42) NOT NULL,
    auction_id BIGINT NOT NULL REFERENCES shadow_auctions(auction_id),
    appeared_in_winner BOOLEAN NOT NULL,
    appeared_in_ours BOOLEAN NOT NULL,
    caused_revert BOOLEAN NOT NULL DEFAULT FALSE,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_token_outcomes_token ON token_outcomes(token_address);
CREATE INDEX ix_token_outcomes_observed ON token_outcomes(observed_at);

-- token feature snapshots (Phase 2)
CREATE TABLE token_features (
    token_address VARCHAR(42) PRIMARY KEY,
    decimals INTEGER,
    contract_verified BOOLEAN,
    has_transfer_tax BOOLEAN,
    bridge_canonical BOOLEAN,
    tvl_usd NUMERIC(20, 2),
    volume_24h_usd NUMERIC(20, 2),
    pool_count_v2 INTEGER,
    pool_count_v3 INTEGER,
    pool_count_camelot INTEGER,
    holder_count INTEGER,
    top10_concentration NUMERIC(5, 4),  -- 0.0 - 1.0
    age_blocks INTEGER,
    on_arbitrum_token_list BOOLEAN,
    on_coingecko BOOLEAN,
    last_refreshed TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

- [ ] **Step 1: Add dependencies**

```toml
# pyproject.toml additions to dependencies
"sqlalchemy[asyncio]>=2.0",
"asyncpg>=0.30",
"alembic>=1.14",
"greenlet>=3.1",
```

Run: `uv sync`

- [ ] **Step 2: Write test for db connection**

```python
# tests/test_persistence/test_db.py
import pytest
from src.persistence.db import get_engine, get_session_factory

async def test_engine_creates(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://solver:solver@localhost:5432/solver_test")
    engine = get_engine()
    assert engine is not None

async def test_session_factory_returns_session():
    factory = get_session_factory()
    async with factory() as session:
        assert session is not None
```

- [ ] **Step 3: Implement `src/persistence/db.py`**

```python
from functools import cache
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from src.config import settings


@cache
def get_engine():
    return create_async_engine(
        settings.database_url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        echo=False,
    )


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)
```

- [ ] **Step 4: Implement ORM models in `src/persistence/models.py`**

```python
from datetime import datetime, UTC
from sqlalchemy import BigInteger, String, Boolean, Integer, Numeric, JSON, Text, DateTime, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ShadowAuction(Base):
    __tablename__ = "shadow_auctions"

    auction_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chain: Mapped[str] = mapped_column(String(20), default="arbitrum_one")
    polled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    n_orders: Mapped[int]
    raw_competition: Mapped[dict] = mapped_column(JSON)
    raw_auction: Mapped[dict] = mapped_column(JSON)


class ShadowSolution(Base):
    __tablename__ = "shadow_solutions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    auction_id: Mapped[int] = mapped_column(ForeignKey("shadow_auctions.auction_id"))
    strategy: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20))
    latency_ms: Mapped[int | None]
    solution: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class ShadowWinner(Base):
    __tablename__ = "shadow_winners"

    auction_id: Mapped[int] = mapped_column(ForeignKey("shadow_auctions.auction_id"), primary_key=True)
    winner_solver: Mapped[str] = mapped_column(Text)
    score: Mapped[int | None] = mapped_column(Numeric(40, 0))
    raw_solution: Mapped[dict] = mapped_column(JSON)


class TokenOutcome(Base):
    __tablename__ = "token_outcomes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_address: Mapped[str] = mapped_column(String(42), index=True)
    auction_id: Mapped[int] = mapped_column(ForeignKey("shadow_auctions.auction_id"))
    appeared_in_winner: Mapped[bool] = mapped_column(Boolean)
    appeared_in_ours: Mapped[bool] = mapped_column(Boolean)
    caused_revert: Mapped[bool] = mapped_column(Boolean, default=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class TokenFeatures(Base):
    __tablename__ = "token_features"

    token_address: Mapped[str] = mapped_column(String(42), primary_key=True)
    decimals: Mapped[int | None]
    contract_verified: Mapped[bool | None]
    has_transfer_tax: Mapped[bool | None]
    bridge_canonical: Mapped[bool | None]
    tvl_usd: Mapped[float | None] = mapped_column(Numeric(20, 2))
    volume_24h_usd: Mapped[float | None] = mapped_column(Numeric(20, 2))
    pool_count_v2: Mapped[int | None]
    pool_count_v3: Mapped[int | None]
    pool_count_camelot: Mapped[int | None]
    holder_count: Mapped[int | None]
    top10_concentration: Mapped[float | None] = mapped_column(Numeric(5, 4))
    age_blocks: Mapped[int | None]
    on_arbitrum_token_list: Mapped[bool | None]
    on_coingecko: Mapped[bool | None]
    last_refreshed: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
```

- [ ] **Step 5: Alembic init + first migration**

```bash
uv run alembic init -t async src/persistence/migrations
# edit alembic.ini script_location = src/persistence/migrations
# edit env.py to import Base from src.persistence.models
uv run alembic revision --autogenerate -m "initial schema"
```

Inspect generated migration, ensure indexes are present.

- [ ] **Step 6: Add DATABASE_URL to config**

```python
# src/config.py
database_url: str = "postgresql+asyncpg://solver:solver@postgres:5432/solver"
```

- [ ] **Step 7: Add Postgres database creation to deploy**

The Hetzner Postgres already runs (used by AI Backoffice). Need a separate database `solver` and user `solver`. One-time setup:

```bash
ssh mounir@116.203.81.107 'docker exec -u postgres postgres psql -c "CREATE USER solver WITH PASSWORD '\''$SOLVER_DB_PASSWORD'\''; CREATE DATABASE solver OWNER solver;"'
```

Add `DATABASE_URL` to GitHub Actions secrets, inject via `docker-compose.yml`.

- [ ] **Step 8: Run migration on server**

```bash
ssh mounir@116.203.81.107 'docker exec cow-solver uv run alembic upgrade head'
```

- [ ] **Step 9: Commit**

```bash
git add src/persistence tests/test_persistence pyproject.toml uv.lock alembic.ini
git commit -m "feat(persistence): postgres schema for shadow data + token features"
git push
```

---

### Task 1.2: Solver writes auction + solution to Postgres

**Files:**
- Create: `src/shadow/persist.py`
- Modify: `src/main.py` (call persist after each /solve)
- Modify: `src/solver/orchestrator.py` (track per-strategy latency + status)

- [ ] **Step 1: Test for persistence helper**

```python
# tests/test_shadow/test_persist.py
import pytest
from unittest.mock import AsyncMock
from src.shadow.persist import persist_shadow_attempt
from src.models.auction import Auction
from src.models.solution import Solution

async def test_persist_writes_auction_and_solutions(db_session):
    auction = Auction(id="1234", tokens={}, orders=[], liquidity=[], effectiveGasPrice=0, deadline=None)
    attempts = [
        {"strategy": "naive", "status": "solved", "latency_ms": 312, "solution": {"id": 1234, "prices": {}, "trades": []}, "error": None}
    ]
    raw_competition = {"auctionId": "1234"}
    await persist_shadow_attempt(db_session, auction, attempts, raw_competition)
    # verify rows
    result = await db_session.execute("SELECT count(*) FROM shadow_auctions")
    assert result.scalar() == 1
    result = await db_session.execute("SELECT count(*) FROM shadow_solutions")
    assert result.scalar() == 1
```

- [ ] **Step 2: Implement `src/shadow/persist.py`**

```python
from datetime import datetime, UTC
from sqlalchemy.dialects.postgresql import insert
from src.persistence.models import ShadowAuction, ShadowSolution
from src.models.auction import Auction


async def persist_shadow_attempt(
    session,
    auction: Auction,
    attempts: list[dict],
    raw_competition: dict,
    raw_auction: dict | None = None,
) -> None:
    """Upsert auction + insert per-strategy solution attempts."""
    auction_row = {
        "auction_id": int(auction.id),
        "polled_at": datetime.now(UTC),
        "n_orders": len(auction.orders),
        "raw_competition": raw_competition,
        "raw_auction": raw_auction or auction.model_dump(mode="json"),
    }
    stmt = insert(ShadowAuction).values(**auction_row).on_conflict_do_nothing()
    await session.execute(stmt)

    for a in attempts:
        session.add(ShadowSolution(
            auction_id=int(auction.id),
            strategy=a["strategy"],
            status=a["status"],
            latency_ms=a.get("latency_ms"),
            solution=a.get("solution"),
            error=a.get("error"),
        ))
    await session.commit()
```

- [ ] **Step 3: Orchestrator returns per-strategy outcomes**

Refactor `SolverOrchestrator.solve()` to return both the chosen solution AND a list of attempt records. New signature:

```python
async def solve(self, auction: Auction) -> tuple[Solution | NoSolution, list[dict]]:
```

Each strategy gets timed, status tracked. The first non-empty Solution wins. Update all callers.

- [ ] **Step 4: /solve handler persists in background**

```python
# src/main.py
@app.post("/solve")
async def solve(auction: Auction, background_tasks: BackgroundTasks):
    solution, attempts = await orchestrator.solve(auction)
    background_tasks.add_task(
        persist_shadow_attempt_safe, auction, attempts, raw_competition={}
    )
    return solution if isinstance(solution, Solution) else {}
```

`persist_shadow_attempt_safe` wraps `persist_shadow_attempt` and never raises (logs and swallows). The /solve hot path must not block on DB.

- [ ] **Step 5: Tests pass + commit**

```bash
uv run pytest tests/test_shadow/test_persist.py tests/test_solver -v
git add src/shadow/persist.py src/main.py src/solver/orchestrator.py tests/
git commit -m "feat(shadow): persist solver attempts to postgres"
```

---

### Task 1.3: Shadow poller writes to Postgres + populates winner

**Files:**
- Modify: `scripts/shadow_poller.py`

The poller already calls `/solve` and writes JSONL. Now: also write the **winner solution** to `shadow_winners` and the raw competition to `shadow_auctions.raw_competition`. The /solve handler already writes `shadow_auctions` row via persist — poller only adds the winner side.

- [ ] **Step 1: Test for winner persistence**

```python
# tests/test_scripts/test_shadow_poller.py (add to existing)
async def test_persist_winner_extracts_solver_and_score():
    from scripts.shadow_poller import _persist_winner
    comp = {
        "auctionId": "9999",
        "solutions": [
            {"solver": "0xabc", "score": "1000000000000000000", "isWinner": True, "ranking": 1, "prices": {}, "trades": []}
        ],
    }
    # call _persist_winner with mocked session, assert insert called with right args
    ...
```

- [ ] **Step 2: Add `_persist_winner` in shadow_poller**

```python
from src.persistence.db import get_session_factory
from src.persistence.models import ShadowWinner
from sqlalchemy.dialects.postgresql import insert as pg_insert


async def _persist_winner(auction_id: int, comp: dict) -> None:
    solutions = comp.get("solutions") or []
    winner = next((s for s in solutions if s.get("isWinner") or s.get("ranking") == 1), None)
    if winner is None:
        return
    Session = get_session_factory()
    async with Session() as session:
        stmt = pg_insert(ShadowWinner).values(
            auction_id=auction_id,
            winner_solver=winner["solver"],
            score=int(winner["score"]) if winner.get("score") else None,
            raw_solution=winner,
        ).on_conflict_do_nothing()
        await session.execute(stmt)
        await session.commit()
```

Call after solver POST in `poll_once`.

- [ ] **Step 3: Tests + commit**

```bash
uv run pytest tests/test_scripts -v
git add scripts/shadow_poller.py tests/test_scripts/
git commit -m "feat(shadow): persist winner solutions to postgres"
```

---

### Task 1.4: Token outcomes extraction

**Files:**
- Create: `src/shadow/token_outcomes.py`
- Modify: `scripts/shadow_poller.py`

For each auction: extract the set of tokens that appeared in (a) the winner solution and (b) our solution. Insert rows in `token_outcomes`. This feeds Phase 2 classifier labels.

- [ ] **Step 1: Test**

```python
# tests/test_shadow/test_token_outcomes.py
from src.shadow.token_outcomes import extract_token_outcomes

def test_extracts_winner_tokens():
    auction = {"orders": [{"sellToken": "0xa", "buyToken": "0xb"}]}
    winner = {"prices": {"0xa": "1", "0xb": "1"}, "trades": []}
    ours = {"prices": {"0xa": "1"}, "trades": []}
    outcomes = extract_token_outcomes(auction, winner, ours)
    assert any(o["token_address"] == "0xa" and o["appeared_in_winner"] for o in outcomes)
    assert any(o["token_address"] == "0xb" and o["appeared_in_winner"] for o in outcomes)
    assert not any(o["token_address"] == "0xb" and o["appeared_in_ours"] for o in outcomes)
```

- [ ] **Step 2: Implement extractor**

```python
# src/shadow/token_outcomes.py
def extract_token_outcomes(
    auction: dict, winner: dict | None, ours: dict | None
) -> list[dict]:
    """For each token in auction.orders, mark if it appeared in winner / ours."""
    tokens = set()
    for o in auction.get("orders", []):
        tokens.add(o["sellToken"].lower())
        tokens.add(o["buyToken"].lower())

    winner_tokens = set((winner or {}).get("prices", {}).keys())
    ours_tokens = set((ours or {}).get("prices", {}).keys())
    winner_tokens = {t.lower() for t in winner_tokens}
    ours_tokens = {t.lower() for t in ours_tokens}

    return [
        {
            "token_address": t,
            "appeared_in_winner": t in winner_tokens,
            "appeared_in_ours": t in ours_tokens,
            "caused_revert": False,
        }
        for t in tokens
    ]
```

- [ ] **Step 3: Insert outcomes in shadow_poller**

After winner persistence, call extractor with auction + winner + ours, bulk-insert into `token_outcomes`.

- [ ] **Step 4: Commit**

```bash
git add src/shadow/token_outcomes.py scripts/shadow_poller.py tests/
git commit -m "feat(shadow): per-token outcomes for classifier training data"
```

---

### Task 1.5: Local AMM router (UniV2 / Camelot V2)

**Goal:** Replace pure naive (reference-price-only) solver with a real AMM router. This is the **Phase 1 architecture pivot** — naive is no longer "fallback only," it's a workhorse.

**Files:**
- Create: `src/routing/multicall.py`
- Create: `src/routing/amm_v2.py`
- Create: `src/routing/multihop.py`
- Create: `src/solver/router.py` (new strategy)
- Modify: `src/solver/orchestrator.py` (add RouterSolver between matching and naive)

Scope for Phase 1:
- UniV2-style pool reads via Multicall3
- 2-hop max routing (Phase 2 extends to 3-hop and adds V3)
- Slippage-aware quotes against pool reserves at current block

#### 1.5a Multicall3 wrapper

- [ ] **Step 1: Test for multicall batching**

```python
# tests/test_routing/test_multicall.py
import pytest
from unittest.mock import AsyncMock
from src.routing.multicall import Multicall3

async def test_batches_calls_and_returns_results():
    mc = Multicall3(rpc=AsyncMock())
    mc.rpc.eth_call = AsyncMock(return_value="0x" + "00" * 32 + "01" * 32)
    results = await mc.aggregate([
        {"target": "0xpool1", "callData": "0x0902f1ac"},  # getReserves
        {"target": "0xpool2", "callData": "0x0902f1ac"},
    ])
    assert len(results) == 2
```

- [ ] **Step 2: Implement Multicall3**

```python
# src/routing/multicall.py
from typing import Any
from eth_abi import encode, decode
from src.routing.rpc import RpcClient

MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"  # Arbitrum
# function aggregate3((address,bool,bytes)[]) returns ((bool,bytes)[])
AGGREGATE3_SELECTOR = "0x82ad56cb"


class Multicall3:
    def __init__(self, rpc: RpcClient, address: str = MULTICALL3_ADDRESS):
        self.rpc = rpc
        self.address = address

    async def aggregate(self, calls: list[dict]) -> list[bytes]:
        tuples = [(c["target"], c.get("allow_failure", False), bytes.fromhex(c["callData"][2:])) for c in calls]
        encoded_args = encode(["(address,bool,bytes)[]"], [tuples])
        data = AGGREGATE3_SELECTOR + encoded_args.hex()
        result_hex = await self.rpc.eth_call(self.address, "0x" + data)
        result_bytes = bytes.fromhex(result_hex[2:])
        decoded = decode(["(bool,bytes)[]"], result_bytes)[0]
        return [bytes(d[1]) for d in decoded]
```

#### 1.5b UniV2 pool reader

- [ ] **Step 3: Test**

```python
# tests/test_routing/test_amm_v2.py
from src.routing.amm_v2 import quote_v2_swap, encode_get_reserves

def test_quote_uses_constant_product_with_fee():
    # Reserves: 100 WETH, 200000 USDC, fee 30bps
    out = quote_v2_swap(amount_in=1_000_000_000_000_000_000, reserve_in=100*10**18, reserve_out=200_000*10**6, fee_bps=30)
    # Expected: roughly 1990 USDC (with slippage + fee)
    assert 1_900_000_000 < out < 2_000_000_000
```

- [ ] **Step 4: Implement quote_v2_swap**

```python
# src/routing/amm_v2.py
def quote_v2_swap(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int = 30) -> int:
    """Constant-product swap with fee. UniV2 formula."""
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    amount_in_with_fee = amount_in * (10_000 - fee_bps)
    numerator = amount_in_with_fee * reserve_out
    denominator = reserve_in * 10_000 + amount_in_with_fee
    return numerator // denominator


def encode_get_reserves() -> str:
    return "0x0902f1ac"  # getReserves() selector


async def fetch_reserves(multicall: Multicall3, pool_addresses: list[str]) -> dict[str, tuple[int, int]]:
    calls = [{"target": p, "callData": encode_get_reserves()} for p in pool_addresses]
    results = await multicall.aggregate(calls)
    reserves = {}
    for addr, raw in zip(pool_addresses, results):
        r0 = int.from_bytes(raw[0:32], "big")
        r1 = int.from_bytes(raw[32:64], "big")
        reserves[addr] = (r0, r1)
    return reserves
```

#### 1.5c Pool registry (which pool for token pair)

- [ ] **Step 5: Hardcode known factory + helper**

For Phase 1, hardcode UniV2-clone factories on Arbitrum:
- SushiSwap V2: `0xc35DADB65012eC5796536bD9864eD8773aBc74C4`
- Camelot V2: `0x6EcCab422D763aC031210895C81787E87B43A652`

```python
# src/routing/amm_v2.py (extend)
FACTORIES_V2 = {
    "sushi": "0xc35DADB65012eC5796536bD9864eD8773aBc74C4",
    "camelot": "0x6EcCab422D763aC031210895C81787E87B43A652",
}

async def get_pool_address(multicall: Multicall3, factory: str, token_a: str, token_b: str) -> str | None:
    # getPair(address,address) → 0xe6a43905
    encoded_a = bytes.fromhex(token_a[2:].zfill(64))
    encoded_b = bytes.fromhex(token_b[2:].zfill(64))
    call_data = "0xe6a43905" + encoded_a.hex() + encoded_b.hex()
    [raw] = await multicall.aggregate([{"target": factory, "callData": call_data}])
    addr = "0x" + raw[12:32].hex()
    if int(addr, 16) == 0:
        return None
    return addr
```

#### 1.5d Multi-hop router (2 hops)

- [ ] **Step 6: Implement DFS over candidate paths**

```python
# src/routing/multihop.py
from dataclasses import dataclass

INTERMEDIATE_TOKENS = [
    "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",  # WETH
    "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # USDC native
    "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",  # USDT
]

@dataclass
class HopQuote:
    pool: str
    token_in: str
    token_out: str
    amount_in: int
    amount_out: int


async def quote_best_path(
    multicall, token_in: str, token_out: str, amount_in: int
) -> list[HopQuote] | None:
    """Try direct + 2-hop paths through INTERMEDIATE_TOKENS. Return best by amount_out."""
    candidates: list[list[HopQuote]] = []
    # Direct
    direct = await _quote_single_hop(multicall, token_in, token_out, amount_in)
    if direct:
        candidates.append([direct])
    # 2-hop via each intermediate
    for mid in INTERMEDIATE_TOKENS:
        if mid in (token_in, token_out):
            continue
        h1 = await _quote_single_hop(multicall, token_in, mid, amount_in)
        if h1 is None:
            continue
        h2 = await _quote_single_hop(multicall, mid, token_out, h1.amount_out)
        if h2 is None:
            continue
        candidates.append([h1, h2])
    if not candidates:
        return None
    return max(candidates, key=lambda path: path[-1].amount_out)
```

`_quote_single_hop` iterates factories, picks pool with best reserves×reserves product (a rough liquidity proxy), and calls `quote_v2_swap`.

#### 1.5e RouterSolver strategy

- [ ] **Step 7: Implement strategy**

```python
# src/solver/router.py
from src.solver.base import NoSolution
from src.models.solution import Solution, Trade
from src.routing.multihop import quote_best_path

class RouterSolver:
    name = "router-v2"

    def __init__(self, multicall):
        self.multicall = multicall

    async def solve(self, auction):
        trades = []
        prices = {}
        for order in auction.orders:
            if order.kind != "sell":
                continue
            path = await quote_best_path(
                self.multicall, order.sell_token, order.buy_token, order.sell_amount
            )
            if path is None:
                continue
            executed_buy = path[-1].amount_out
            if executed_buy < order.buy_amount:
                continue
            # Implied clearing price ratio
            prices[order.sell_token] = executed_buy
            prices[order.buy_token] = order.sell_amount
            trades.append(Trade(kind="fulfillment", order_uid=order.uid, executed_amount=order.sell_amount))
        if not trades:
            return NoSolution()
        return Solution(id=int(auction.id), prices=prices, trades=trades, interactions=[])
```

- [ ] **Step 8: Wire into orchestrator**

Chain order: `[CoWMatchingSolver, LongTailRouter, RouterSolver, NaiveSolver]`

- [ ] **Step 9: Integration test against fixture auction**

Use a captured Arbitrum auction (`tests/fixtures/arbitrum_auction_*.json`) with mocked RPC returning known reserves. Assert RouterSolver returns a Solution with all orders filled and surplus delta positive vs naive.

- [ ] **Step 10: Commit**

```bash
git add src/routing/ src/solver/router.py src/solver/orchestrator.py tests/test_routing/
git commit -m "feat(routing): UniV2 multi-hop router as Phase 1 workhorse strategy"
git push
```

---

### Task 1.6: Surplus analyzer

**Files:**
- Modify: `src/shadow/analyzer.py` (already exists for JSONL; extend to Postgres)
- Create: `src/shadow/cli.py` (CLI entrypoint)

- [ ] **Step 1: Test surplus calculation**

```python
# tests/test_shadow/test_analyzer.py
from src.shadow.analyzer import compute_surplus_delta

def test_surplus_delta_positive_when_we_beat_winner():
    ours = {"prices": {"WETH": 4000_000_000, "USDC": 1_000_000}, "trades": [...]}
    winner = {"prices": {"WETH": 3990_000_000, "USDC": 1_000_000}, "trades": [...]}
    delta = compute_surplus_delta(ours, winner, reference_prices={"WETH": 4000, "USDC": 1})
    assert delta > 0
```

- [ ] **Step 2: Implement surplus delta**

Surplus = `Σ (executed_buy - required_buy) × reference_price[buy_token]` across all trades, normalized to numéraire (USD via reference_price). Delta = ours - winner.

- [ ] **Step 3: CLI to report aggregate stats**

```bash
uv run python -m src.shadow.cli analyze --since "7 days ago"
```

Output:
```
Batches analyzed:        823
Solved (any strategy):   801 (97.3%)
Strategy breakdown:
  router-v2:             612 (76.4%)
  naive:                 189 (23.6%)
Hypothetical wins (Δ>0):  21 (2.6%)
Mean surplus delta:      -0.0028 USD (we lose on average)
```

- [ ] **Step 4: Commit**

```bash
git add src/shadow/analyzer.py src/shadow/cli.py tests/
git commit -m "feat(shadow): postgres-backed surplus analyzer CLI"
```

---

### Task 1.7: Phase 1 exit gate

Run for 7+ days, then verify:

- [ ] ≥500 unique auctions in `shadow_auctions`
- [ ] ≥500 corresponding rows in `shadow_winners`
- [ ] ≥1000 rows in `shadow_solutions` (multiple strategies per auction)
- [ ] CLI report shows non-zero surplus deltas (positive or negative — important: solver is producing comparable solutions, not always NoSolution)
- [ ] p95 `latency_ms` < 5s for `router-v2` strategy
- [ ] No memory leaks: container RSS stable over 24h
- [ ] Disk usage <90% sustained

**If hypothetical-win-rate is already ≥2% at end of Phase 1: bullish signal for Phase 2.**
**If 0% wins across 500+ batches: investigate route bugs / quote bugs before adding edge.**

---

## Phase 2: Edge Implementation (4-6 weeks, ~60-100 h)

Goal: Hypothetical win-rate ≥5% on Arbitrum shadow data. Router upgraded to UniV3 + concentrated liquidity. LightGBM classifier in the loop. CoW-matching layered on top with proper LP + CIP-67 composer.

### Task 2.1: UniV3 quoter with tick math

Arbitrum volume sits in UniV3 / Camelot V3 (Algebra). Quote-by-simulation through ticks is non-trivial — use the official Uniswap V3 SDK port for Python (`uniswap-python` is unmaintained; recommended: port the JS QuoterV2 logic or use `pyuniswap` only as reference).

**Architectural decision:** Use **QuoterV2 contract** via `eth_call` for accuracy (no local tick-math reimplementation). Tradeoff: each quote is one RPC call. With Multicall3 we batch them — still cheaper than rolling our own tick math and getting edge cases wrong.

QuoterV2 on Arbitrum: `0x61fFE014bA17989E743c5F6cB21bF9697530B21e`

**Files:**
- Create: `src/routing/amm_v3.py`
- Create: `src/routing/quoter_v2.py`
- Modify: `src/routing/multihop.py` (mix V2 + V3 hops)

Tasks:
- [ ] Encode `quoteExactInputSingle(QuoteExactInputSingleParams)` calldata
- [ ] Decode return: `(amountOut, sqrtPriceX96After, initializedTicksCrossed, gasEstimate)`
- [ ] Batch via Multicall3 with `allow_failure=true` (quotes can revert if no liquidity)
- [ ] Iterate fee tiers `[100, 500, 3000, 10000]`
- [ ] Extend `multihop.quote_best_path` to consider V2 + V3 + Camelot V3 candidates per hop
- [ ] Tests with captured pool state fixtures

**Acceptance:** Router beats `naive` by ≥30 bps surplus on 70%+ of shadow batches.

---

### Task 2.2: Pool indexer (long-tail surface)

**Files:**
- Replace stub: `edge/pool_indexer/__init__.py`
- Create: `edge/pool_indexer/factory_watcher.py`
- Create: `edge/pool_indexer/pool_cache.py`
- Create: `edge/pool_indexer/liquidity_scorer.py`

Architecture:
- WebSocket subscription to Arbitrum factory events (`PairCreated`, `PoolCreated`) on UniV2, UniV3, Camelot V2/V3
- New pool → Redis cache with key `solver:pool:{address}` (TTL 24h, refreshed on reads)
- Liquidity scorer: rough TVL via reserves × reference price (USD)
- Surface API: `find_pools_for_pair(token_in, token_out) -> list[PoolInfo]`

Tasks:
- [ ] Factory event ABI encoding
- [ ] WebSocket reconnect logic with backoff
- [ ] Pool cache eviction (LRU + TTL combo)
- [ ] Scorer ranks pools by USD liquidity
- [ ] Integration with `multihop.quote_best_path` — query indexer for non-canonical pools

**Acceptance:** Router finds ≥10% more profitable paths in shadow batches than without indexer.

---

### Task 2.3: LightGBM classifier pipeline

This is the multi-task slice. Build in order:

#### 2.3a Feature extractor

**Files:**
- Create: `edge/classifier/feature_extractor.py`
- Add cron service: `cow-feature-extractor` (separate container)

Tasks:
- [ ] Query unenriched tokens from `token_outcomes`
- [ ] For each: RPC Multicall for `decimals`, `totalSupply`, `balanceOf(top10 holders)` (Etherscan API for top holders list), pool counts per factory
- [ ] Honeypot heuristic: simulate `transfer(self, 0)` + `transfer(self, amount)` via `eth_call` — if reverts with non-standard error, flag
- [ ] CoinGecko API for listing status + 24h volume (with rate-limit handling)
- [ ] Arbitrum bridge canonical check: query `L2GatewayRouter.calculateL2TokenAddress(l1_token)` and compare
- [ ] Write to `token_features` (upsert, refresh `last_refreshed`)
- [ ] Backfill mode: process 100 tokens per 6h tick

**Acceptance:** ≥80% of tokens seen in last 30 days have non-null `tvl_usd`, `pool_count_v3`, `contract_verified`.

#### 2.3b Label generator

**Files:**
- Create: `edge/classifier/label_generator.py`

Rules (start simple, iterate):

| Label | Rule |
|---|---|
| `legit` | ≥5 occurrences in winning solutions in last 30 days AND no reverts |
| `scam` | ≥2 reverts in last 30 days OR honeypot heuristic positive |
| `unknown` | otherwise |

Tasks:
- [ ] SQL query joining `token_outcomes` + `token_features`. Reverts come from `token_outcomes.caused_revert` (column already in schema from Task 1.1, populated by Phase 4 reconciler from on-chain Settlement events).
- [ ] Output: pandas DataFrame for trainer
- [ ] Class balance report: log distribution before training
- [ ] Until Phase 4 reconciler runs: `caused_revert` will be all-false. Labels in Phase 2 rely on positive signal (`appeared_in_winner`) + honeypot heuristic from `token_features.has_transfer_tax`. Revert-based negatives kick in Phase 4.

#### 2.3c Trainer

**Files:**
- Create: `edge/classifier/train.py`
- Create: `edge/classifier/evaluate.py`
- Create: `edge/classifier/model_registry.py`

Tasks:
- [ ] Stratified train/test split (80/20)
- [ ] Cold-start: `RandomForestClassifier` if `n_samples < 2000`, else `LGBMClassifier`
- [ ] Hyperparameters: as documented in conversation (num_leaves=31, max_depth=6, min_data_in_leaf=20, lr=0.05, n_estimators=500 with early stopping)
- [ ] Eval: ROC-AUC + F1 + confusion matrix; log via structlog
- [ ] Save: `model_v{ts}.pkl`, update `current.pkl` symlink only if AUC improvement >0.01
- [ ] Slack/Sentry notification on promotion
- [ ] CLI: `uv run python -m edge.classifier.train --weeks-back 8`

#### 2.3d Runtime predictor

**Files:**
- Create: `edge/classifier/predict.py`

Tasks:
- [ ] `TokenClassifier` class: loads `current.pkl` on init, watches file via inotify for hot-reload (or restart-only for Phase 2; hot-reload Phase 3)
- [ ] `score_token(token_address) -> float` returns P(legit) ∈ [0, 1]
- [ ] Feature lookup: Redis cache (TTL 1h) → Postgres `token_features` → if missing, default to median features + log `unenriched_token`
- [ ] Integration in pool_indexer.liquidity_scorer: score < 0.3 → exclude from routing

#### 2.3e Trainer cron container

**Files:**
- Modify: `deploy/hetzner/cow-solver/docker-compose.yml`

Add a service `cow-trainer` with cron entrypoint (e.g., supercronic), running `edge.classifier.train` weekly. RAM limit 1.5G (peak during training).

**Acceptance gate for Task 2.3:**
- [ ] First model trained with ≥500 labeled samples (mixed labels)
- [ ] Predictor integrated, no /solve latency regression (p95 budget +50ms allowed)
- [ ] Manual eval: top-10 highest-confidence scams in test set are actual scams (sanity check via Arbiscan)

---

### Task 2.4: Real LP in `_solve_ring_lp`

**Files:**
- Modify: `edge/matching/multi_party.py`
- Create: `edge/matching/surplus.py` (LP formulation isolated)

The current stub fills at `sell_amount` and ignores ring balance. Replace with scipy `linprog` HiGHS:

```
Variables: x_i = executed_sell_i per order, p_t = clearing_price per token
Objective: maximize sum of surplus_i = (x_i * p_sell - executed_buy_i) (approx)
Constraints:
  - 0 <= x_i <= sell_amount_i
  - For each token t in ring: sum_inflow == sum_outflow (ring balance)
  - executed_buy_i = x_i * p_sell / p_buy >= buy_amount_i (limit)
  - p_t > 0
```

Linearize via fixed price ratios (use auction reference prices as anchor, search small ε-perturbations).

Tasks:
- [ ] Test against 3-order ring with known optimal fill
- [ ] Test against infeasible ring (returns None)
- [ ] Test against 4-order ring with one binding limit (partial fill)
- [ ] Performance test: 100 rings × LP each < 500ms total

**Acceptance:** Matching strategy now produces non-degenerate trades on ≥30% of multi-ring auctions.

---

### Task 2.5: Bipartite matcher

**Files:**
- Create: `edge/matching/bipartite.py`
- Modify: `edge/matching/__init__.py` (export `BipartiteMatcher`)
- Modify: `src/solver/orchestrator.py` (add bipartite before multi-party)

Direct 2-party CoW: find order pairs `A→B` and `B→A` where limit prices overlap. Far more common than 3+ party rings and cheap to compute. O(N) sweep with token-pair dictionary.

Tasks:
- [ ] Build `(sell_token, buy_token) -> [orders]` index
- [ ] For each `(A, B)` group, check `(B, A)` group exists
- [ ] Find max fill respecting both limits
- [ ] Emit Trade pairs with clearing price between both limits
- [ ] Tests

**Acceptance:** ≥15% of shadow batches produce bipartite matches with positive surplus delta.

---

### Task 2.6: CIP-67 composer

**Files:**
- Create: `edge/matching/composer.py`

Current behavior: each strategy returns independent Solutions, orchestrator picks first non-empty. CIP-67 requires **uniform clearing prices per directed token pair** across the whole batch. This means composing solutions from multiple strategies/rings into one coherent Solution.

Tasks:
- [ ] Detect price conflicts across candidate trades
- [ ] LP-merge: variables = uniform clearing prices per token; constraints = each candidate trade's limit feasibility; objective = total surplus
- [ ] If LP infeasible, drop lowest-surplus candidates iteratively
- [ ] Replace orchestrator's first-non-empty logic with composer

**Acceptance:** Submitted Solutions never violate CIP-67 (verified by composer self-check); surplus on multi-strategy batches improves ≥10% over single-strategy-first.

---

### Task 2.7: Phase 2 exit gate

Run for 2+ weeks with full edge stack enabled, then measure:

- [ ] Hypothetical win-rate ≥5% on rolling 14-day window
- [ ] Per-stage p95 latency within budget (Prometheus histograms required):

  | Stage | Budget (spec §3.5) | Acceptance |
  |---|---|---|
  | Auction parse | 50 ms | p95 < 100 ms |
  | Bipartite + multi-party matching (LP+rings) | 2 s | p95 < 2.5 s |
  | Long-tail pool lookup (cache hit) | 1 s | p95 < 1 s, cache hit-rate > 70% |
  | Router (UniV2+V3 multi-hop) | 3 s | p95 < 3.5 s |
  | Composer + encoding | 500 ms | p95 < 750 ms |
  | **Total /solve** | **15 s hard** | **p95 < 8 s, p99 < 12 s** |

- [ ] Classifier auto-retrains weekly without manual intervention
- [ ] No /solve errors over 24h sustained
- [ ] RAM stable (no leaks across 7-day continuous run)

**If win-rate <2%: STOP — re-evaluate architecture before KYC.** This is the brutal stop condition from the spec.

**If win-rate 2-5%: continue but flag risk** — Phase 4 may need JIT liquidity to lift to 5%+.

**If win-rate ≥5%: proceed to Phase 3.**

---

## Phase 3: Onboarding Prep (1-2 weeks)

Goal: All artifacts ready for the CoW Team onboarding call, server topology decision locked in, no surprises in code review.

### Task 3.1: Phase 2 profiling → topology decision

Run 7 consecutive days under full Phase 2 load with profiling enabled:

- [ ] Peak RAM (RSS) of solver + trainer + indexer (separate metrics)
- [ ] Peak CPU during /solve under load
- [ ] Postgres disk growth rate (GB/week)
- [ ] Redis memory usage

Decision criteria:

| Outcome | Decision |
|---|---|
| Peak solver RAM <2 GB AND backoffice can stay on | Stay on CX22, deploy production |
| Peak solver RAM 2-3 GB OR backoffice unstable | Upgrade to CX32 (€8 → €15/mo) |
| Peak >3 GB OR Postgres contention with backoffice | Own VM (CX22 dedicated for solver, €8/mo extra) |

Document decision in `docs/superpowers/decisions/phase3-topology.md`.

### Task 3.2: KYC artifact preparation

User-tasks (cannot be automated, per spec §8):
- [ ] Passport scan (high-res, color, all four corners visible)
- [ ] Optional: Gewerbenachweis (if operating as registered business — improves bonding terms)
- [ ] Decide on entity: personal vs Einzelunternehmen vs GmbH (tax implications outside this plan's scope)

Code-side prep:
- [ ] README cleanup: ensure architecture diagram matches reality
- [ ] Code-review prep document `docs/CODE_REVIEW.md`:
  - Public/private split rationale
  - Edge algorithm overview (high-level, for CoW Team)
  - Latency budget table (measured, not target)
  - Security considerations (RPC handling, key management, no on-chain submission yet)
- [ ] Add LICENSE to public repo (MIT or AGPL — decide based on commercial intent)

### Task 3.3: Rewards wallet

User-tasks:
- [ ] Create fresh MetaMask account (NOT reuse existing)
- [ ] Hardware wallet integration (Ledger recommended)
- [ ] Document address in private notes, share with CoW Team during onboarding

### Task 3.4: Operational runbooks

**Files:**
- Create: `docs/runbooks/solver-down.md`
- Create: `docs/runbooks/high-latency.md`
- Create: `docs/runbooks/classifier-rollback.md`
- Create: `docs/runbooks/deploy-rollback.md`

Each runbook: symptoms, dashboards to check, commands to run, escalation. Format: under 1 page, scannable in 30 seconds at 3am.

### Task 3.5: Telegram outreach + onboarding call

User-tasks:
- [ ] Message `t.me/cowprotocol` (or current onboarding channel — verify current process at docs.cow.fi)
- [ ] Mention: solver name, Arbitrum target, Shadow data available for review
- [ ] Schedule call (typically 30-60 min, technical + KYC walkthrough)
- [ ] Prepare to grant CoW Team read-access to private `cow-solver-edge` repo (security review, not algorithm theft per spec §6)

### Task 3.6: Phase 3 exit gate

- [ ] Onboarding call completed
- [ ] CoW Team has reviewed code (private + public)
- [ ] Topology decision implemented (server upgraded/split if needed)
- [ ] All runbooks reviewed by yourself + ideally one external reader for clarity

---

## Phase 4: Staging (Barn) (2-4 weeks)

Goal: 50+ on-chain settlements in Barn without errors, latency p95 <12s, monitoring + alerting tight, ready for production traffic.

### Task 4.1: Driver setup by CoW Team

CoW Team provisions a production-like driver pointing at your solver. They'll provide:
- [ ] Production driver config (`config/driver.production.toml`)
- [ ] Submission key location (CoW-managed, not your concern)
- [ ] Barn endpoint URL

Your side:
- [ ] Expose solver via Caddy: `solver.mhagenticworld.de` → `cow-solver:8000`
- [ ] TLS via existing Caddy + Let's Encrypt setup
- [ ] Firewall: only CoW driver IPs (provided by team) + your monitoring
- [ ] Auth: bearer token if requested by team

### Task 4.2: Production-grade RPC

Public `arb1.arbitrum.io/rpc` was OK for shadow; Barn settlement requires <500ms p95 RPC latency.

Decision:
- [ ] Alchemy Growth tier (€49/mo) OR
- [ ] Infura Developer tier (€50/mo) OR
- [ ] Self-host Arbitrum Nitro node (€20-30/mo on dedicated server but high ops cost)

Recommendation: Alchemy. Use WebSocket for factory events, HTTPS for `eth_call`.

- [ ] Add `RPC_ARBITRUM_PROD` secret to GHA
- [ ] Update solver config to use prod RPC, keep public as fallback

### Task 4.3: Settlement reconciler

Reconciler runs but currently does nothing. In Barn, settlements happen on-chain — we need to verify our submitted solutions actually settled and update `token_outcomes` with revert info.

**Files:**
- Modify: `scripts/reconcile_loop.py`
- Create: `src/persistence/models.py` add `Settlement` model

Tasks:
- [ ] Query `Settlement` events from CoW contract (`0x9008D19f58AAbD9eD0D60971565AA8510560ab41` — same on all chains)
- [ ] Match by `auction_id` (in event log topics)
- [ ] Update `shadow_solutions` row with `on_chain_status: settled | reverted | not_included`
- [ ] If reverted: identify failing token, write to `failed_settlements` for classifier

### Task 4.4: Grafana dashboard

**Files:**
- Create: `deploy/hetzner/grafana/dashboards/cow_solver.json` (provisioned via existing Grafana setup)

Panels (one row each):
- [ ] Win-rate (hypothetical Phase 2, real settlements Phase 4) — line chart, 7-day rolling
- [ ] /solve latency histogram — p50/p95/p99
- [ ] Per-stage latency breakdown (parse, matching, routing, composer) — stacked
- [ ] Strategy outcome distribution — % solved vs no-solution vs error per strategy
- [ ] Surplus delta vs winner — histogram + 7-day median
- [ ] Settlement success rate (Phase 4) — gauge + trend
- [ ] Classifier metrics — current AUC, prediction distribution histogram
- [ ] Resource usage — CPU/RAM/disk for `cow-solver`, `cow-trainer`, Postgres

Use existing Prometheus datasource. Add scrape target `cow-solver:8001` to `prometheus.yml` if not already present.

Acceptance: dashboard loads in <2s, all panels show non-empty data within 24h of deploy.

### Task 4.5: Alerting

**Files:**
- Modify: `deploy/hetzner/prometheus/solver-rules.yml`

Add rules:
- [ ] /solve p99 latency > 14s for 5 min → page
- [ ] /solve error rate > 5% over 10 min → page
- [ ] Settlement-revert rate > 2% over 1 hour → page
- [ ] Solver container OOM-killed → page
- [ ] Postgres connection pool exhausted → warn
- [ ] Disk >85% → warn, >95% → page
- [ ] No /solve calls received in 10 min → warn (driver issue)

Page channel: Telegram bot or Pushover (already used for backoffice?). User decides.

### Task 4.6: Barn iteration

For the first 50 settlements:
- [ ] Daily review: surplus delta, settlement success rate, latency
- [ ] Tune classifier thresholds based on Barn data (real settlements = real labels)
- [ ] Tune router gas estimation (Barn has real gas costs affecting scoring)
- [ ] Watch for edge cases: very small orders, very large orders, exotic tokens

### Task 4.7: Phase 4 exit gate

- [ ] 50+ successful Barn settlements
- [ ] Settlement-failure rate <5%
- [ ] p95 /solve latency <12s sustained
- [ ] Zero OOM kills in last 7 days
- [ ] CoW Team approves promotion to production

---

## Cross-Phase Concerns

### Frequent commit policy

Each task above ends with a commit. **Push frequently** — at minimum daily, ideally per task. GitHub Actions builds and deploys automatically; broken builds are caught early.

Never bundle multiple architectural changes in one commit. Reviewers (including future-self and CoW Team) need clean diffs.

### Testing discipline

- **Unit tests:** Pure functions, mocked I/O. Required for all new code in `src/`, `edge/`.
- **Integration tests:** Hit real Postgres (test database via docker-compose.test.yml) and mocked RPC. Required for persistence + routing.
- **Shadow integration tests:** Captured real auctions in `tests/fixtures/`. Required before any orchestrator/composer change.
- **No production deployments without green CI.** GitHub Actions enforces this.

### What NOT to do during Phase 1-4

- **No on-chain submissions.** Phase 4 is Barn (test settlement), not production. CoW Team manages submission keys.
- **No private orderflow / RFQ integrations.** Out of scope until post-production.
- **No multi-chain.** Base / Gnosis are post-launch.
- **No clever optimization without measurement.** If a "perf improvement" isn't backed by p95 numbers from shadow data, don't merge it.
- **No Claude/Anthropic in the solver code path.** (Per user constraint: Groq for any LLM-adjacent task; ideally no LLM in solver path at all — classifier is gradient-boosted.)

### Estimated total effort

| Phase | Hours (solo, part-time) | Calendar weeks (10-15h/wk) |
|---|---|---|
| 0' Stabilization | 4-6 | 0.5 |
| 1 Shadow + Router | 25-40 | 2-3 |
| 2 Edge + Classifier | 60-100 | 4-6 |
| 3 Onboarding Prep | 10-15 | 1-2 |
| 4 Barn | 20-30 | 2-4 |
| **Total** | **120-190** | **10-15 weeks** |

Full-time: 4-6 weeks. Part-time at 10-15h/wk: 3-4 months calendar.

---

## Decision Log

Decisions made during this plan (record changes here as they happen):

| Date | Decision | Rationale | Reversible? |
|---|---|---|---|
| 2026-05-22 | Router-primary architecture | Empirical surplus distribution favors routing; pure CoW-matching caps at ~3% win-rate | Yes (re-order strategies) |
| 2026-05-22 | LightGBM for token classifier | Determinism, sub-ms inference, no external API dependency | Yes (swap model class) |
| 2026-05-22 | RustWorkX for cycle enumeration | 10-100× speedup over networkx within latency budget | Yes |
| 2026-05-22 | QuoterV2 contract call over local tick math | Accuracy > latency in Phase 2 (still well within budget) | Yes |
| 2026-05-22 | scipy.linprog HiGHS for ring LP | Standard, well-tested, fast enough at small variable counts | Yes |

---

## Risks to Watch

1. **Shadow data quality:** if shadow_poller misses >20% of auctions due to rate limits, training data is biased. Monitor coverage weekly.
2. **Postgres on shared box:** if backoffice resumes prod under the solver, Postgres I/O contention could blow latency budget. Topology decision in Phase 3 must address.
3. **Classifier feedback loop:** if classifier wrongly drops a legit token, we lose those batches → fewer labels for retraining → drift compounds. Always keep a low-confidence "shadow track" (route anyway, log outcome, don't act on classifier in 5% of cases) to break the loop.
4. **CIP-67 protocol updates:** Fair Combinatorial Auction is recent and may evolve. Subscribe to CoW governance forum.
5. **Solver-template-py drift:** the reference Python solver may lag protocol changes. We use it only for ABI references — keep an eye on changelog.
