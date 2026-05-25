# Phase 0 + 1: Skeleton + Shadow Naive — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy a working naive CoW solver in Shadow mode on Hetzner against real Arbitrum orderflow, collecting batch data and surplus deltas vs the actual winning solver. Endpoint of this plan = "data is flowing, analyzer works, ready to start Phase 2 (edge implementation)."

**Architecture:** Public Python/FastAPI skeleton with `/solve` endpoint that wraps 1inch as a naive baseline. Private git submodule reserved for the edge algorithm (empty in this plan). Shadow driver (CoW's Rust binary) runs on the same Hetzner host and calls the solver via the docker network. Solution logger writes both your responses and the winning solutions to disk; analyzer computes surplus deltas.

**Tech Stack:**
- Python 3.12, uv (dep manager), FastAPI, Pydantic v2, httpx (async HTTP)
- pytest + pytest-asyncio + respx (HTTP mocking)
- ruff (lint), mypy (typecheck)
- structlog (logging), prometheus-client (metrics), pydantic-settings (config)
- Docker multi-stage build, docker-compose, GitHub Actions
- redis-py (re-use existing Backoffice Redis with `solver:` prefix)

**Spec reference:** [docs/superpowers/specs/2026-05-22-cow-solver-design.md](../specs/2026-05-22-cow-solver-design.md)

---

## File Structure

This plan creates the following structure:

```
cow-solver-skeleton/                  # PUBLIC repo
├── README.md
├── .gitignore
├── .python-version                   # 3.12
├── pyproject.toml                    # uv-managed deps
├── Dockerfile                        # multi-stage Python build
├── docker-compose.yml                # local dev: solver + shadow driver
├── .github/workflows/
│   ├── ci.yml                        # lint, test, typecheck
│   └── deploy.yml                    # build → GHCR → SSH Hetzner
├── config/
│   ├── driver.shadow.toml            # CoW driver config for shadow mode
│   └── driver.staging.toml.example   # template for Phase 4
├── src/
│   ├── __init__.py
│   ├── main.py                       # FastAPI app, /solve, /health, /metrics
│   ├── config.py                     # pydantic-settings: env-driven config
│   ├── log.py                        # structlog setup
│   ├── models/
│   │   ├── __init__.py
│   │   ├── auction.py                # Auction (CoW input schema)
│   │   ├── order.py                  # Order
│   │   └── solution.py               # Solution (CoW output schema)
│   ├── solver/
│   │   ├── __init__.py
│   │   ├── base.py                   # SolverStrategy protocol + Solution helpers
│   │   ├── naive.py                  # NaiveSolver (1inch wrapper)
│   │   └── orchestrator.py           # chains strategies; loads edge if present
│   ├── routing/
│   │   ├── __init__.py
│   │   ├── oneinch.py                # 1inch v6 client (async)
│   │   └── rpc.py                    # ethers-py / web3 client wrapper
│   ├── shadow/
│   │   ├── __init__.py
│   │   ├── logger.py                 # SolutionLogger (JSONL append)
│   │   └── analyzer.py               # SurplusAnalyzer CLI
│   └── metrics.py                    # Prometheus counters/histograms
├── edge/                             # git submodule (empty placeholder for now)
│   └── .gitkeep
├── tests/
│   ├── __init__.py
│   ├── conftest.py                   # fixtures: sample auctions, mock clients
│   ├── fixtures/
│   │   └── sample_auction.json       # real CoW batch sample
│   ├── test_models/
│   │   ├── test_auction.py
│   │   ├── test_order.py
│   │   └── test_solution.py
│   ├── test_solver/
│   │   ├── test_naive.py
│   │   └── test_orchestrator.py
│   ├── test_routing/
│   │   ├── test_oneinch.py
│   │   └── test_rpc.py
│   ├── test_shadow/
│   │   ├── test_logger.py
│   │   └── test_analyzer.py
│   └── test_main.py                  # FastAPI integration tests
└── docs/
    ├── DEPLOYMENT.md                 # how to deploy / debug
    └── superpowers/{specs,plans}/    # already exists
```

**Responsibility per file:**
- `main.py`: HTTP transport layer only — parses request, hands to orchestrator, returns response.
- `config.py`: single source of truth for env vars; never read `os.environ` elsewhere.
- `models/*.py`: pure data shapes matching CoW Protocol JSON schema. No I/O.
- `solver/base.py`: `SolverStrategy` Protocol + a `NoSolution` sentinel.
- `solver/naive.py`: implements `SolverStrategy`, uses 1inch client. No business logic about CoW matching.
- `solver/orchestrator.py`: chains strategies, handles timeouts per strategy, falls back gracefully.
- `routing/oneinch.py`: 1inch HTTP client. No solver logic.
- `routing/rpc.py`: Web3 RPC client (block number, gas price). Used by NaiveSolver for gas estimates.
- `shadow/logger.py`: appends `{auction_id, your_solution, winner_solution, surplus_delta}` to JSONL.
- `shadow/analyzer.py`: CLI that reads JSONL and prints stats.
- `metrics.py`: only Prometheus instruments; no business logic.

---

## Pre-flight: things the user must do BEFORE starting

These cannot be automated and block specific tasks:

| # | What | Required for task | How |
|---|------|-------------------|-----|
| P1 | Sign up for **1inch API key** at portal.1inch.dev | Task 9 | Free tier, ~2min |
| P2 | Choose **Arbitrum RPC URL** (start with `https://arb1.arbitrum.io/rpc` — public) | Task 10 | Just note the URL; upgrade to Alchemy later |
| P3 | Decide **GitHub org/user** for repos | Task 1 | Confirmed: `mounirhajri` |
| P4 | Approve `gh repo create` for two repos | Task 1 | One-line confirmation |
| P5 | Generate a **deploy key** for private edge submodule access | Task 24 | Script in Task 24 |
| P6 | Add **Hetzner secrets** to GitHub Actions secrets | Task 26 | 3 secrets, ~5min |
| P7 | **Stop AI Backoffice** on Hetzner before first solver deploy | Task 27 | One SSH command |

Defer P5–P7 until those tasks. Get P1 and P2 done early — they take 2 minutes and unblock Tasks 9–10.

---

## Task 1: Initialize repositories and project scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `.python-version`
- Create: `.gitignore`
- Create: `README.md`
- Create: `src/__init__.py`
- Create: `tests/__init__.py`
- Create: `edge/.gitkeep`

- [ ] **Step 1: Confirm gh CLI access**

Run:
```bash
cd /Users/mma/workspace/cow-solver-skeleton
gh auth status
```
Expected: `Logged in to github.com account mounirhajri`. Stop if not.

- [ ] **Step 2: Create `.python-version`**

```
3.12
```

- [ ] **Step 3: Create `pyproject.toml`**

```toml
[project]
name = "cow-solver-skeleton"
version = "0.1.0"
description = "CoW Protocol solver — public skeleton"
requires-python = ">=3.12,<3.13"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "httpx>=0.27",
    "structlog>=24.4",
    "prometheus-client>=0.21",
    "redis>=5.2",
    "web3>=7.6",
    "tenacity>=9.0",
]

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "pytest-cov>=6.0",
    "respx>=0.21",
    "ruff>=0.7",
    "mypy>=1.13",
    "types-redis>=4.6",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM"]

[tool.mypy]
python_version = "3.12"
strict = true
plugins = ["pydantic.mypy"]
```

- [ ] **Step 4: Create `.gitignore`**

```
__pycache__/
*.py[cod]
.venv/
.env
.env.local
*.egg-info/
dist/
build/
.pytest_cache/
.coverage
htmlcov/
.mypy_cache/
.ruff_cache/
shadow_data/
*.jsonl
.DS_Store
```

- [ ] **Step 5: Create `README.md`**

```markdown
# cow-solver-skeleton

Public skeleton for a CoW Protocol solver. The competitive edge lives in a
private git submodule under `edge/` and is loaded at runtime if present.

See [design spec](docs/superpowers/specs/2026-05-22-cow-solver-design.md).

## Quick start

```bash
uv sync
uv run pytest
uv run uvicorn --factory src.main:build_default_app --reload
```

## Deployment

See [DEPLOYMENT.md](docs/DEPLOYMENT.md).
```

- [ ] **Step 6: Create empty package markers**

```bash
mkdir -p src tests edge
touch src/__init__.py tests/__init__.py edge/.gitkeep
```

- [ ] **Step 7: Install uv and sync dependencies**

Run:
```bash
which uv || curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
```
Expected: `.venv/` created, all deps resolved.

- [ ] **Step 8: Verify imports**

Run: `uv run python -c "import fastapi, pydantic, httpx, web3, redis; print('OK')"`
Expected: `OK`

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "chore: scaffold cow-solver-skeleton with uv + python 3.12"
```

- [ ] **Step 10: Create GitHub repos and push**

Confirm with user before running:
```bash
gh repo create mounirhajri/cow-solver-skeleton --public --source=. --remote=origin --push
gh repo create mounirhajri/cow-solver-edge --private
```
Expected: both repos created, skeleton pushed to main.

---

## Task 2: Auction/Order/Solution Pydantic models — write tests first

**Files:**
- Create: `tests/fixtures/sample_auction.json`
- Create: `tests/test_models/__init__.py`
- Create: `tests/test_models/test_auction.py`

CoW Protocol schema reference: https://docs.cow.fi/cow-protocol/reference/core/auctions/schema

- [ ] **Step 1: Create `tests/fixtures/sample_auction.json`**

This is a minimal but valid CoW auction matching the schema. The driver sends this shape to `/solve`.

```json
{
  "id": "12345",
  "tokens": {
    "0x82af49447d8a07e3bd95bd0d56f35241523fbab1": {
      "decimals": 18,
      "symbol": "WETH",
      "referencePrice": "3500000000000000000000",
      "availableBalance": "10000000000000000000",
      "trusted": true
    },
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831": {
      "decimals": 6,
      "symbol": "USDC",
      "referencePrice": "1000000000000000000",
      "availableBalance": "100000000000",
      "trusted": true
    }
  },
  "orders": [
    {
      "uid": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "sellToken": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
      "buyToken": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
      "sellAmount": "1000000000000000000",
      "buyAmount": "3450000000",
      "feePolicies": [],
      "validTo": 9999999999,
      "kind": "sell",
      "owner": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "partiallyFillable": false,
      "class": "market"
    }
  ],
  "liquidity": [],
  "effectiveGasPrice": "100000000",
  "deadline": "2026-05-22T12:00:00Z",
  "surplusCapturingJitOrderOwners": []
}
```

(uid is 56 bytes = 112 hex chars; owner is 20 bytes = 40 hex chars. Copy these strings literally — they're already valid JSON.)

- [ ] **Step 2: Create `tests/test_models/__init__.py`** (empty)

- [ ] **Step 3: Write failing test for Auction parsing**

`tests/test_models/test_auction.py`:
```python
import json
from pathlib import Path

import pytest

from src.models.auction import Auction


@pytest.fixture
def sample_auction_dict() -> dict:
    path = Path(__file__).parent.parent / "fixtures" / "sample_auction.json"
    return json.loads(path.read_text())


def test_auction_parses_minimal_valid_input(sample_auction_dict: dict) -> None:
    auction = Auction.model_validate(sample_auction_dict)
    assert auction.id == "12345"
    assert len(auction.orders) == 1
    assert auction.orders[0].kind == "sell"
    assert auction.orders[0].sell_amount == 10**18


def test_auction_rejects_missing_id(sample_auction_dict: dict) -> None:
    del sample_auction_dict["id"]
    with pytest.raises(ValueError):
        Auction.model_validate(sample_auction_dict)


def test_auction_tokens_indexed_by_address(sample_auction_dict: dict) -> None:
    auction = Auction.model_validate(sample_auction_dict)
    weth = auction.tokens["0x82af49447d8a07e3bd95bd0d56f35241523fbab1"]
    assert weth.decimals == 18
    assert weth.symbol == "WETH"
```

- [ ] **Step 4: Run test, verify it fails**

Run: `uv run pytest tests/test_models/test_auction.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.models'`

- [ ] **Step 5: Create model files**

`src/models/__init__.py`: (empty)

`src/models/order.py`:
```python
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FeePolicy(BaseModel):
    """Fee policy attached to an order. We pass through opaquely."""

    model_config = ConfigDict(extra="allow")


class Order(BaseModel):
    """A CoW order in an auction.

    Wire amounts (sellAmount, buyAmount) are decimal strings of base units
    in the JSON. We parse them as int.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    uid: str
    sell_token: str = Field(alias="sellToken")
    buy_token: str = Field(alias="buyToken")
    sell_amount: int = Field(alias="sellAmount")
    buy_amount: int = Field(alias="buyAmount")
    fee_policies: list[FeePolicy] = Field(alias="feePolicies", default_factory=list)
    valid_to: int = Field(alias="validTo")
    kind: Literal["sell", "buy"]
    owner: str
    partially_fillable: bool = Field(alias="partiallyFillable")
    class_: Literal["market", "limit", "liquidity"] = Field(alias="class")
```

`src/models/auction.py`:
```python
from pydantic import BaseModel, ConfigDict, Field

from src.models.order import Order


class Token(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    decimals: int
    symbol: str | None = None
    reference_price: int | None = Field(alias="referencePrice", default=None)
    available_balance: int = Field(alias="availableBalance", default=0)
    trusted: bool = False


class Auction(BaseModel):
    """A CoW auction batch as delivered to /solve."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    tokens: dict[str, Token]
    orders: list[Order]
    liquidity: list[dict] = Field(default_factory=list)
    effective_gas_price: int = Field(alias="effectiveGasPrice", default=0)
    deadline: str | None = None
```

- [ ] **Step 6: Run test, verify it passes**

Run: `uv run pytest tests/test_models/test_auction.py -v`
Expected: 3 passed.

- [ ] **Step 7: Commit**

```bash
git add src/models tests/test_models tests/fixtures
git commit -m "feat(models): Pydantic models for Auction, Order, Token with tests"
```

---

## Task 3: Solution model

**Files:**
- Create: `src/models/solution.py`
- Create: `tests/test_models/test_solution.py`

- [ ] **Step 1: Write failing test**

`tests/test_models/test_solution.py`:
```python
from src.models.solution import Solution, Trade


def test_solution_serializes_to_cow_schema() -> None:
    sol = Solution(
        id=1,
        prices={"0x82af49447d8a07e3bd95bd0d56f35241523fbab1": 3500 * 10**18},
        trades=[
            Trade(
                kind="fulfillment",
                order_uid="0x" + "a" * 112,
                executed_amount=10**18,
            )
        ],
        interactions=[],
    )
    payload = sol.model_dump(by_alias=True, mode="json")
    assert payload["id"] == 1
    assert payload["trades"][0]["kind"] == "fulfillment"
    assert payload["trades"][0]["orderUid"] == "0x" + "a" * 112
    assert payload["trades"][0]["executedAmount"] == str(10**18)


def test_empty_solution_is_valid() -> None:
    sol = Solution(id=1, prices={}, trades=[], interactions=[])
    payload = sol.model_dump(by_alias=True, mode="json")
    assert payload["trades"] == []
```

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/test_models/test_solution.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Create `src/models/solution.py`**

```python
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class Trade(BaseModel):
    """An executed order in a solution."""

    model_config = ConfigDict(populate_by_name=True)

    kind: Literal["fulfillment", "jit"]
    order_uid: str = Field(alias="orderUid")
    executed_amount: int = Field(alias="executedAmount")

    @field_serializer("executed_amount")
    def serialize_amount(self, v: int) -> str:
        return str(v)


class Solution(BaseModel):
    """A CoW solution returned from /solve."""

    model_config = ConfigDict(populate_by_name=True)

    id: int
    prices: dict[str, int]
    trades: list[Trade]
    interactions: list[dict]

    @field_serializer("prices")
    def serialize_prices(self, v: dict[str, int]) -> dict[str, str]:
        return {k: str(val) for k, val in v.items()}
```

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/test_models/test_solution.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/models/solution.py tests/test_models/test_solution.py
git commit -m "feat(models): Solution and Trade models with CoW-schema serialization"
```

---

## Task 4: Config module (env-driven settings)

**Files:**
- Create: `src/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write failing test**

`tests/test_config.py`:
```python
import pytest

from src.config import Settings


def test_settings_loads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEINCH_API_KEY", "test-key")
    monkeypatch.setenv("RPC_ARBITRUM", "https://arb1.example/rpc")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/3")
    monkeypatch.setenv("SHADOW_LOG_PATH", "/tmp/shadow.jsonl")
    s = Settings()
    assert s.oneinch_api_key == "test-key"
    assert s.rpc_arbitrum == "https://arb1.example/rpc"
    assert s.redis_url == "redis://localhost:6379/3"
    assert s.redis_key_prefix == "solver:"  # default
    assert s.pool_cache_max_entries == 800  # default for shadow phase
    assert str(s.shadow_log_path) == "/tmp/shadow.jsonl"
```

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL.

- [ ] **Step 3: Create `src/config.py`**

```python
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All env-driven configuration. Read once at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # External APIs
    oneinch_api_key: str = ""
    rpc_arbitrum: str = "https://arb1.arbitrum.io/rpc"

    # Cache
    redis_url: str = "redis://localhost:6379/3"
    redis_key_prefix: str = "solver:"
    pool_cache_max_entries: int = 800
    pool_cache_ttl_seconds: int = 60

    # Behaviour
    amm_state_lazy: bool = True
    solve_timeout_seconds: float = 13.0  # slightly below CoW's 15s deadline

    # Shadow logging
    shadow_log_path: Path = Path("./shadow_data/log.jsonl")

    # Observability
    log_level: str = "INFO"
    prometheus_port: int = 8001


settings = Settings()
```

Also at the top of the file, add: `from pathlib import Path`.

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/test_config.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/config.py tests/test_config.py
git commit -m "feat(config): env-driven Settings via pydantic-settings"
```

---

## Task 5: Structured logging setup

**Files:**
- Create: `src/log.py`
- Create: `tests/test_log.py`

- [ ] **Step 1: Write failing test**

`tests/test_log.py`:
```python
import io
import json

from src.log import configure_logging, get_logger


def test_logger_emits_structured_json(monkeypatch) -> None:
    buf = io.StringIO()
    configure_logging(level="DEBUG", stream=buf)
    log = get_logger("test")
    log.info("hello", auction_id="42")
    output = buf.getvalue().strip()
    payload = json.loads(output)
    assert payload["event"] == "hello"
    assert payload["auction_id"] == "42"
    assert payload["level"] == "info"
```

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/test_log.py -v`
Expected: FAIL.

- [ ] **Step 3: Create `src/log.py`**

```python
import logging
import sys
from typing import IO

import structlog


def configure_logging(level: str = "INFO", stream: IO[str] | None = None) -> None:
    """Configure structlog to emit JSON to stdout (or supplied stream)."""

    out_stream = stream or sys.stdout

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(level)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=out_stream),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
```

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/test_log.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/log.py tests/test_log.py
git commit -m "feat(log): structlog JSON logging"
```

---

## Task 6: SolverStrategy protocol and NoSolution sentinel

**Files:**
- Create: `src/solver/__init__.py`
- Create: `src/solver/base.py`
- Create: `tests/test_solver/__init__.py`
- Create: `tests/test_solver/test_base.py`

- [ ] **Step 1: Write failing test**

`tests/test_solver/test_base.py`:
```python
from typing import get_type_hints

from src.models.auction import Auction
from src.models.solution import Solution
from src.solver.base import NoSolution, SolverStrategy


def test_solver_strategy_is_protocol() -> None:
    # Protocols are runtime_checkable when decorated; we just assert callable
    assert hasattr(SolverStrategy, "solve")


def test_no_solution_is_falsy() -> None:
    assert not NoSolution()


def test_solver_strategy_signature() -> None:
    hints = get_type_hints(SolverStrategy.solve)
    assert hints["auction"] is Auction
    assert hints["return"] == Solution | NoSolution
```

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/test_solver/test_base.py -v`
Expected: FAIL.

- [ ] **Step 3: Create files**

`src/solver/__init__.py`: (empty)

`src/solver/base.py`:
```python
from typing import Protocol, runtime_checkable

from src.models.auction import Auction
from src.models.solution import Solution


class NoSolution:
    """Sentinel returned when a strategy has no solution for this auction."""

    def __bool__(self) -> bool:
        return False


@runtime_checkable
class SolverStrategy(Protocol):
    """Interface every solver strategy must implement."""

    name: str

    async def solve(self, auction: Auction) -> Solution | NoSolution:
        ...
```

`tests/test_solver/__init__.py`: (empty)

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/test_solver/test_base.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/solver tests/test_solver
git commit -m "feat(solver): SolverStrategy protocol and NoSolution sentinel"
```

---

## Task 7: 1inch async client

**Files:**
- Create: `src/routing/__init__.py`
- Create: `src/routing/oneinch.py`
- Create: `tests/test_routing/__init__.py`
- Create: `tests/test_routing/test_oneinch.py`

API reference: https://portal.1inch.dev/documentation/swap/v6.0/swap

- [ ] **Step 1: Write failing test**

`tests/test_routing/test_oneinch.py`:
```python
import httpx
import pytest
import respx

from src.routing.oneinch import OneInchClient, OneInchQuote


@respx.mock
async def test_quote_returns_parsed_response() -> None:
    respx.get("https://api.1inch.dev/swap/v6.0/42161/quote").mock(
        return_value=httpx.Response(
            200,
            json={
                "dstAmount": "3450000000",
                "gas": 200000,
            },
        )
    )
    client = OneInchClient(api_key="test", chain_id=42161)
    quote = await client.quote(
        src="0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
        dst="0xaf88d065e77c8cc2239327c5edb3a432268e5831",
        amount=10**18,
    )
    assert isinstance(quote, OneInchQuote)
    assert quote.dst_amount == 3_450_000_000
    assert quote.gas == 200000
    await client.close()


@respx.mock
async def test_quote_raises_on_5xx() -> None:
    respx.get("https://api.1inch.dev/swap/v6.0/42161/quote").mock(
        return_value=httpx.Response(503, json={"error": "service unavailable"})
    )
    client = OneInchClient(api_key="test", chain_id=42161)
    with pytest.raises(httpx.HTTPStatusError):
        await client.quote(
            src="0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
            dst="0xaf88d065e77c8cc2239327c5edb3a432268e5831",
            amount=10**18,
        )
    await client.close()
```

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/test_routing/test_oneinch.py -v`
Expected: FAIL.

- [ ] **Step 3: Create files**

`src/routing/__init__.py`: (empty)
`tests/test_routing/__init__.py`: (empty)

`src/routing/oneinch.py`:
```python
from dataclasses import dataclass

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


@dataclass(frozen=True)
class OneInchQuote:
    dst_amount: int
    gas: int


class OneInchClient:
    """Async client for 1inch swap API v6."""

    BASE_URL = "https://api.1inch.dev/swap/v6.0"

    def __init__(self, api_key: str, chain_id: int, timeout: float = 5.0) -> None:
        self._chain_id = chain_id
        self._client = httpx.AsyncClient(
            base_url=f"{self.BASE_URL}/{chain_id}",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
        )

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.ReadTimeout)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.2, max=1.0),
    )
    async def quote(self, src: str, dst: str, amount: int) -> OneInchQuote:
        resp = await self._client.get(
            "/quote",
            params={"src": src, "dst": dst, "amount": str(amount)},
        )
        resp.raise_for_status()
        data = resp.json()
        return OneInchQuote(dst_amount=int(data["dstAmount"]), gas=int(data.get("gas", 0)))

    async def close(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/test_routing/test_oneinch.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/routing tests/test_routing
git commit -m "feat(routing): 1inch async client with retry"
```

---

## Task 8: RPC client (gas price)

**Files:**
- Create: `src/routing/rpc.py`
- Create: `tests/test_routing/test_rpc.py`

- [ ] **Step 1: Write failing test**

`tests/test_routing/test_rpc.py`:
```python
from unittest.mock import MagicMock, patch

from src.routing.rpc import RpcClient


def test_gas_price_returns_int_wei() -> None:
    fake_web3 = MagicMock()
    fake_web3.eth.gas_price = 100_000_000  # 0.1 gwei
    with patch("src.routing.rpc.Web3", return_value=fake_web3):
        client = RpcClient("https://rpc.example")
        assert client.gas_price() == 100_000_000


def test_block_number() -> None:
    fake_web3 = MagicMock()
    fake_web3.eth.block_number = 12345
    with patch("src.routing.rpc.Web3", return_value=fake_web3):
        client = RpcClient("https://rpc.example")
        assert client.block_number() == 12345
```

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/test_routing/test_rpc.py -v`
Expected: FAIL.

- [ ] **Step 3: Create `src/routing/rpc.py`**

```python
from web3 import Web3
from web3.providers.rpc import HTTPProvider


class RpcClient:
    """Minimal sync RPC client for L2 reads. Web3.py is sync; we accept the cost."""

    def __init__(self, url: str) -> None:
        self._w3 = Web3(HTTPProvider(url, request_kwargs={"timeout": 5}))

    def gas_price(self) -> int:
        return int(self._w3.eth.gas_price)

    def block_number(self) -> int:
        return int(self._w3.eth.block_number)
```

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/test_routing/test_rpc.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/routing/rpc.py tests/test_routing/test_rpc.py
git commit -m "feat(routing): sync RPC client for gas + block number"
```

---

## Task 9: Naive solver — wraps 1inch as fallback

**Requires:** P1 (1inch API key). If not yet, use stubbed key for tests; real key for shadow run.

**Files:**
- Create: `src/solver/naive.py`
- Create: `tests/test_solver/test_naive.py`

- [ ] **Step 1: Write failing test**

`tests/test_solver/test_naive.py`:
```python
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.models.auction import Auction
from src.models.solution import Solution
from src.routing.oneinch import OneInchQuote
from src.solver.base import NoSolution
from src.solver.naive import NaiveSolver


@pytest.fixture
def sample_auction() -> Auction:
    path = Path(__file__).parent.parent / "fixtures" / "sample_auction.json"
    return Auction.model_validate(json.loads(path.read_text()))


async def test_naive_solver_returns_solution(sample_auction: Auction) -> None:
    oneinch = AsyncMock()
    oneinch.quote.return_value = OneInchQuote(dst_amount=3_460_000_000, gas=200_000)
    solver = NaiveSolver(oneinch=oneinch)

    result = await solver.solve(sample_auction)

    assert isinstance(result, Solution)
    assert len(result.trades) == 1
    assert result.trades[0].executed_amount == 10**18
    # prices include both tokens
    assert len(result.prices) == 2


async def test_naive_solver_returns_nosolution_when_below_limit(
    sample_auction: Auction,
) -> None:
    # User wants min 3.45e9 USDC, quote returns 3.4e9 → cannot satisfy
    oneinch = AsyncMock()
    oneinch.quote.return_value = OneInchQuote(dst_amount=3_400_000_000, gas=200_000)
    solver = NaiveSolver(oneinch=oneinch)

    result = await solver.solve(sample_auction)
    assert isinstance(result, NoSolution)


async def test_naive_solver_skips_buy_orders(sample_auction: Auction) -> None:
    sample_auction.orders[0].kind = "buy"
    oneinch = AsyncMock()
    solver = NaiveSolver(oneinch=oneinch)

    result = await solver.solve(sample_auction)
    # naive v1 only handles sell orders
    assert isinstance(result, NoSolution)
    oneinch.quote.assert_not_called()
```

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/test_solver/test_naive.py -v`
Expected: FAIL.

- [ ] **Step 3: Create `src/solver/naive.py`**

```python
from src.log import get_logger
from src.models.auction import Auction
from src.models.solution import Solution, Trade
from src.routing.oneinch import OneInchClient
from src.solver.base import NoSolution

log = get_logger(__name__)


class NaiveSolver:
    """Wraps 1inch to quote each sell order independently.

    This is a baseline only. It does NO CoW-matching and treats each order
    as if it's the only one in the batch. Its purpose is to give us a working
    fallback while we develop the real edge in the private submodule.
    """

    name = "naive"

    def __init__(self, oneinch: OneInchClient) -> None:
        self._oneinch = oneinch

    async def solve(self, auction: Auction) -> Solution | NoSolution:
        trades: list[Trade] = []
        prices: dict[str, int] = {}

        for order in auction.orders:
            if order.kind != "sell":
                log.debug("skip_non_sell_order", uid=order.uid, kind=order.kind)
                continue

            try:
                quote = await self._oneinch.quote(
                    src=order.sell_token,
                    dst=order.buy_token,
                    amount=order.sell_amount,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("oneinch_quote_failed", uid=order.uid, error=str(e))
                continue

            if quote.dst_amount < order.buy_amount:
                log.debug(
                    "below_limit",
                    uid=order.uid,
                    quoted=quote.dst_amount,
                    required=order.buy_amount,
                )
                continue

            trades.append(
                Trade(kind="fulfillment", order_uid=order.uid, executed_amount=order.sell_amount)
            )
            # Uniform clearing price per directed pair (sell/buy ratio)
            prices[order.sell_token] = quote.dst_amount
            prices[order.buy_token] = order.sell_amount

        if not trades:
            return NoSolution()

        return Solution(id=int(auction.id), prices=prices, trades=trades, interactions=[])
```

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/test_solver/test_naive.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/solver/naive.py tests/test_solver/test_naive.py
git commit -m "feat(solver): NaiveSolver wraps 1inch as per-order baseline"
```

---

## Task 10: Strategy orchestrator with timeout per strategy

**Files:**
- Create: `src/solver/orchestrator.py`
- Create: `tests/test_solver/test_orchestrator.py`

- [ ] **Step 1: Write failing test**

`tests/test_solver/test_orchestrator.py`:
```python
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.models.auction import Auction
from src.models.solution import Solution, Trade
from src.solver.base import NoSolution
from src.solver.orchestrator import SolverOrchestrator


@pytest.fixture
def auction() -> Auction:
    path = Path(__file__).parent.parent / "fixtures" / "sample_auction.json"
    return Auction.model_validate(json.loads(path.read_text()))


async def test_orchestrator_returns_first_solution(auction: Auction) -> None:
    sol = Solution(id=1, prices={}, trades=[Trade(kind="fulfillment", order_uid="0x" + "a"*112, executed_amount=10**18)], interactions=[])
    s1 = AsyncMock(name="s1")
    s1.name = "s1"
    s1.solve.return_value = sol
    s2 = AsyncMock(name="s2")
    s2.name = "s2"

    orch = SolverOrchestrator(strategies=[s1, s2], per_strategy_timeout=1.0)
    result = await orch.solve(auction)

    assert result is sol
    s2.solve.assert_not_called()  # s1 already returned solution


async def test_orchestrator_falls_through_on_nosolution(auction: Auction) -> None:
    s1 = AsyncMock(name="s1")
    s1.name = "s1"
    s1.solve.return_value = NoSolution()
    sol = Solution(id=1, prices={}, trades=[Trade(kind="fulfillment", order_uid="0x" + "a"*112, executed_amount=10**18)], interactions=[])
    s2 = AsyncMock(name="s2")
    s2.name = "s2"
    s2.solve.return_value = sol

    orch = SolverOrchestrator(strategies=[s1, s2], per_strategy_timeout=1.0)
    result = await orch.solve(auction)
    assert result is sol


async def test_orchestrator_times_out_slow_strategy(auction: Auction) -> None:
    async def slow_solve(a: Auction) -> Solution | NoSolution:
        await asyncio.sleep(10)
        raise AssertionError("should have been cancelled")

    s1 = AsyncMock(name="s1")
    s1.name = "slow"
    s1.solve.side_effect = slow_solve

    sol = Solution(id=1, prices={}, trades=[Trade(kind="fulfillment", order_uid="0x" + "a"*112, executed_amount=10**18)], interactions=[])
    s2 = AsyncMock(name="s2")
    s2.name = "fast"
    s2.solve.return_value = sol

    orch = SolverOrchestrator(strategies=[s1, s2], per_strategy_timeout=0.1)
    result = await orch.solve(auction)
    assert result is sol


async def test_orchestrator_returns_nosolution_if_all_fail(auction: Auction) -> None:
    s1 = AsyncMock(name="s1")
    s1.name = "s1"
    s1.solve.return_value = NoSolution()
    orch = SolverOrchestrator(strategies=[s1], per_strategy_timeout=1.0)
    result = await orch.solve(auction)
    assert isinstance(result, NoSolution)
```

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/test_solver/test_orchestrator.py -v`
Expected: FAIL.

- [ ] **Step 3: Create `src/solver/orchestrator.py`**

```python
import asyncio
from collections.abc import Sequence

from src.log import get_logger
from src.models.auction import Auction
from src.models.solution import Solution
from src.solver.base import NoSolution, SolverStrategy

log = get_logger(__name__)


class SolverOrchestrator:
    """Tries strategies in order; returns first non-empty solution.

    Each strategy is bounded by per_strategy_timeout. Strategies that exceed
    the timeout are cancelled and the orchestrator falls through to the next.
    """

    def __init__(
        self,
        strategies: Sequence[SolverStrategy],
        per_strategy_timeout: float = 5.0,
    ) -> None:
        if not strategies:
            raise ValueError("at least one strategy required")
        self._strategies = list(strategies)
        self._timeout = per_strategy_timeout

    async def solve(self, auction: Auction) -> Solution | NoSolution:
        for strat in self._strategies:
            try:
                result = await asyncio.wait_for(strat.solve(auction), timeout=self._timeout)
            except asyncio.TimeoutError:
                log.warning("strategy_timeout", strategy=strat.name, auction_id=auction.id)
                continue
            except Exception as e:  # noqa: BLE001
                log.error("strategy_error", strategy=strat.name, error=str(e), auction_id=auction.id)
                continue

            if isinstance(result, Solution):
                log.info("strategy_won", strategy=strat.name, auction_id=auction.id)
                return result

        return NoSolution()


def load_default_strategies(oneinch_api_key: str) -> list[SolverStrategy]:
    """Build the strategy chain. Loads edge strategies if private submodule present.

    Order: edge strategies first (more specialized), naive last (fallback).
    """
    from src.routing.oneinch import OneInchClient
    from src.solver.naive import NaiveSolver

    strategies: list[SolverStrategy] = []

    # Try to load edge submodule
    try:
        from edge.matching import CoWMatchingSolver  # type: ignore[import-not-found]
        from edge.pool_indexer import LongTailRouter  # type: ignore[import-not-found]

        strategies.append(CoWMatchingSolver())
        strategies.append(LongTailRouter())
        log.info("edge_strategies_loaded")
    except ImportError:
        log.info("edge_strategies_not_present", reason="public_clone_or_phase0")

    # Always include naive as last resort
    oneinch = OneInchClient(api_key=oneinch_api_key, chain_id=42161)
    strategies.append(NaiveSolver(oneinch=oneinch))

    return strategies
```

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/test_solver/test_orchestrator.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/solver/orchestrator.py tests/test_solver/test_orchestrator.py
git commit -m "feat(solver): orchestrator with per-strategy timeout and edge loader"
```

---

## Task 11: Prometheus metrics module

**Files:**
- Create: `src/metrics.py`
- Create: `tests/test_metrics.py`

- [ ] **Step 1: Write failing test**

`tests/test_metrics.py`:
```python
from src.metrics import SOLVE_DURATION, SOLVE_TOTAL, STRATEGY_TOTAL


def test_counters_exist() -> None:
    SOLVE_TOTAL.labels(outcome="solution").inc()
    SOLVE_TOTAL.labels(outcome="no_solution").inc()
    STRATEGY_TOTAL.labels(name="naive", outcome="solution").inc()


def test_histogram_exists() -> None:
    SOLVE_DURATION.observe(0.5)
    SOLVE_DURATION.observe(1.5)
```

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/test_metrics.py -v`
Expected: FAIL.

- [ ] **Step 3: Create `src/metrics.py`**

```python
from prometheus_client import Counter, Histogram

SOLVE_TOTAL = Counter(
    "cow_solver_solve_total",
    "Total /solve invocations by outcome.",
    ["outcome"],  # solution | no_solution | error
)

STRATEGY_TOTAL = Counter(
    "cow_solver_strategy_total",
    "Strategy attempts by name and outcome.",
    ["name", "outcome"],  # solution | no_solution | timeout | error
)

SOLVE_DURATION = Histogram(
    "cow_solver_solve_duration_seconds",
    "End-to-end /solve duration.",
    buckets=(0.1, 0.5, 1, 2, 5, 8, 10, 13, 15, 20),
)
```

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/test_metrics.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/metrics.py tests/test_metrics.py
git commit -m "feat(metrics): Prometheus counters and histogram"
```

---

## Task 12: Solution logger (writes auction + solution + winner to JSONL)

**Files:**
- Create: `src/shadow/__init__.py`
- Create: `src/shadow/logger.py`
- Create: `tests/test_shadow/__init__.py`
- Create: `tests/test_shadow/test_logger.py`

The shadow driver doesn't tell us the winner solution directly — we'll need to fetch it from the CoW Orderbook API in Task 14. For now, the logger just stores OUR solution per auction with a placeholder for the winner.

- [ ] **Step 1: Write failing test**

`tests/test_shadow/test_logger.py`:
```python
import json
from pathlib import Path

import pytest

from src.models.solution import Solution, Trade
from src.shadow.logger import SolutionLogger


def test_logger_appends_jsonl(tmp_path: Path) -> None:
    logger = SolutionLogger(path=tmp_path / "shadow.jsonl")
    sol = Solution(id=1, prices={}, trades=[Trade(kind="fulfillment", order_uid="0x" + "a"*112, executed_amount=10**18)], interactions=[])
    logger.record(auction_id="123", our_solution=sol)
    logger.record(auction_id="124", our_solution=None)

    lines = (tmp_path / "shadow.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2

    first = json.loads(lines[0])
    assert first["auction_id"] == "123"
    assert first["our_solution"]["id"] == 1
    assert "timestamp" in first

    second = json.loads(lines[1])
    assert second["our_solution"] is None
```

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/test_shadow/test_logger.py -v`
Expected: FAIL.

- [ ] **Step 3: Create files**

`src/shadow/__init__.py`: (empty)
`tests/test_shadow/__init__.py`: (empty)

`src/shadow/logger.py`:
```python
import json
from datetime import UTC, datetime
from pathlib import Path

from src.models.solution import Solution


class SolutionLogger:
    """Append-only JSONL log of (auction_id, our_solution, winner_solution).

    Winner is filled in later by a background reconciliation task that polls
    the CoW Orderbook API (Task 14).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, auction_id: str, our_solution: Solution | None) -> None:
        record = {
            "auction_id": auction_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "our_solution": our_solution.model_dump(by_alias=True, mode="json") if our_solution else None,
            "winner_solution": None,  # filled later
        }
        with self._path.open("a") as f:
            f.write(json.dumps(record) + "\n")
```

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/test_shadow/test_logger.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/shadow tests/test_shadow
git commit -m "feat(shadow): JSONL solution logger"
```

---

## Task 13: FastAPI app with /solve, /health, /metrics

**Files:**
- Create: `src/main.py`
- Create: `tests/test_main.py`

- [ ] **Step 1: Write failing test**

`tests/test_main.py`:
```python
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
    orch.solve.return_value = Solution(
        id=12345,
        prices={"0x82af49447d8a07e3bd95bd0d56f35241523fbab1": 3500 * 10**18},
        trades=[Trade(kind="fulfillment", order_uid="0x" + "a"*112, executed_amount=10**18)],
        interactions=[],
    )
    app = create_app(orchestrator=orch)
    client = TestClient(app)
    resp = client.post("/solve", json=auction_payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 12345
    assert len(body["trades"]) == 1


def test_solve_with_no_solution(auction_payload: dict) -> None:
    orch = AsyncMock()
    orch.solve.return_value = NoSolution()
    app = create_app(orchestrator=orch)
    client = TestClient(app)
    resp = client.post("/solve", json=auction_payload)
    # CoW driver expects a Solution object; empty solution = no participation
    assert resp.status_code == 200
    body = resp.json()
    assert body["trades"] == []
```

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/test_main.py -v`
Expected: FAIL.

- [ ] **Step 3: Create `src/main.py`**

```python
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from src.config import settings
from src.log import configure_logging, get_logger
from src.metrics import SOLVE_DURATION, SOLVE_TOTAL
from src.models.auction import Auction
from src.models.solution import Solution
from src.shadow.logger import SolutionLogger
from src.solver.base import NoSolution
from src.solver.orchestrator import SolverOrchestrator, load_default_strategies
from pathlib import Path

log = get_logger(__name__)


def create_app(
    orchestrator: SolverOrchestrator | Any,
    shadow_logger: SolutionLogger | None = None,
) -> FastAPI:
    """Factory so tests can inject a mock orchestrator."""

    app = FastAPI(title="cow-solver-skeleton")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/solve")
    async def solve(request: Request) -> dict[str, Any]:
        start = time.perf_counter()
        body = await request.json()
        auction = Auction.model_validate(body)

        try:
            result = await orchestrator.solve(auction)
        except Exception as e:  # noqa: BLE001
            log.error("solve_error", auction_id=auction.id, error=str(e))
            SOLVE_TOTAL.labels(outcome="error").inc()
            return _empty_solution(auction.id)

        if isinstance(result, NoSolution):
            SOLVE_TOTAL.labels(outcome="no_solution").inc()
            if shadow_logger:
                shadow_logger.record(auction_id=auction.id, our_solution=None)
            return _empty_solution(auction.id)

        SOLVE_TOTAL.labels(outcome="solution").inc()
        SOLVE_DURATION.observe(time.perf_counter() - start)
        if shadow_logger:
            shadow_logger.record(auction_id=auction.id, our_solution=result)
        return result.model_dump(by_alias=True, mode="json")

    return app


def _empty_solution(auction_id: str) -> dict[str, Any]:
    return Solution(id=int(auction_id), prices={}, trades=[], interactions=[]).model_dump(
        by_alias=True, mode="json"
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    configure_logging(level=settings.log_level)
    log.info("startup", config=settings.model_dump())
    yield
    log.info("shutdown")


def build_default_app() -> FastAPI:
    """Entry point used by uvicorn in --factory mode.

    Kept as factory (not module-level `app`) so importing this module in tests
    does not trigger filesystem and network side-effects.
    """
    configure_logging(level=settings.log_level)
    strategies = load_default_strategies(settings.oneinch_api_key)
    orchestrator = SolverOrchestrator(
        strategies=strategies,
        per_strategy_timeout=settings.solve_timeout_seconds / max(1, len(strategies)),
    )
    shadow_logger = SolutionLogger(path=settings.shadow_log_path)
    return create_app(orchestrator=orchestrator, shadow_logger=shadow_logger)
```

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/test_main.py -v`
Expected: 4 passed.

- [ ] **Step 5: Local smoke test**

Run:
```bash
ONEINCH_API_KEY=dummy uv run uvicorn --factory src.main:build_default_app --host 127.0.0.1 --port 8000 &
sleep 2
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/metrics | head -10
kill %1
```
Expected: `{"status":"ok"}` then Prometheus output lines. Shadow log defaults to `./shadow_data/log.jsonl` (gitignored).

- [ ] **Step 6: Commit**

```bash
git add src/main.py tests/test_main.py
git commit -m "feat(api): FastAPI app with /solve, /health, /metrics"
```

---

## Task 14: CoW Orderbook client — fetch winner solution for shadow analysis

**Files:**
- Create: `src/shadow/cow_api.py`
- Create: `tests/test_shadow/test_cow_api.py`

API ref: https://api.cow.fi/docs/

- [ ] **Step 1: Write failing test**

`tests/test_shadow/test_cow_api.py`:
```python
import httpx
import respx

from src.shadow.cow_api import CowApiClient, CompetitionResult


@respx.mock
async def test_fetch_competition_returns_winner() -> None:
    respx.get(
        "https://api.cow.fi/arbitrum_one/api/v1/solver_competition/12345"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "auctionId": 12345,
                "solutions": [
                    {"solver": "barter", "ranking": 1, "score": "1000000000"},
                    {"solver": "naive", "ranking": 2, "score": "950000000"},
                ],
            },
        )
    )
    client = CowApiClient(network="arbitrum_one")
    result = await client.fetch_competition(auction_id=12345)
    assert isinstance(result, CompetitionResult)
    assert result.winner_solver == "barter"
    assert result.winner_score == 1_000_000_000
    await client.close()


@respx.mock
async def test_fetch_competition_returns_none_on_404() -> None:
    respx.get(
        "https://api.cow.fi/arbitrum_one/api/v1/solver_competition/999"
    ).mock(return_value=httpx.Response(404))
    client = CowApiClient(network="arbitrum_one")
    result = await client.fetch_competition(auction_id=999)
    assert result is None
    await client.close()
```

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/test_shadow/test_cow_api.py -v`
Expected: FAIL.

- [ ] **Step 3: Create `src/shadow/cow_api.py`**

```python
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class CompetitionResult:
    auction_id: int
    winner_solver: str
    winner_score: int


class CowApiClient:
    """Client for the public CoW Orderbook API.

    Used in shadow phase to fetch the actual winning solver and score per auction,
    so we can compute our surplus delta.
    """

    BASE_BY_NETWORK = {
        "mainnet": "https://api.cow.fi/mainnet/api/v1",
        "arbitrum_one": "https://api.cow.fi/arbitrum_one/api/v1",
        "base": "https://api.cow.fi/base/api/v1",
        "gnosis": "https://api.cow.fi/xdai/api/v1",
    }

    def __init__(self, network: str = "arbitrum_one", timeout: float = 5.0) -> None:
        base = self.BASE_BY_NETWORK[network]
        self._client = httpx.AsyncClient(base_url=base, timeout=timeout)

    async def fetch_competition(self, auction_id: int) -> CompetitionResult | None:
        resp = await self._client.get(f"/solver_competition/{auction_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        solutions = data.get("solutions", [])
        winner = next((s for s in solutions if s.get("ranking") == 1), None)
        if not winner:
            return None
        return CompetitionResult(
            auction_id=int(data["auctionId"]),
            winner_solver=winner["solver"],
            winner_score=int(winner["score"]),
        )

    async def close(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/test_shadow/test_cow_api.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/shadow/cow_api.py tests/test_shadow/test_cow_api.py
git commit -m "feat(shadow): CoW API client for fetching competition winner"
```

---

## Task 15: Shadow analyzer CLI

**Files:**
- Create: `src/shadow/analyzer.py`
- Create: `tests/test_shadow/test_analyzer.py`

- [ ] **Step 1: Write failing test**

`tests/test_shadow/test_analyzer.py`:
```python
import json
from pathlib import Path

from src.shadow.analyzer import analyze


def test_analyzer_reports_basic_stats(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    lines = [
        {"auction_id": "1", "our_solution": {"id": 1, "trades": [{"kind": "fulfillment"}]}, "winner_solution": {"solver": "barter"}, "timestamp": "2026-05-22T12:00:00+00:00"},
        {"auction_id": "2", "our_solution": None, "winner_solution": {"solver": "rizzolver"}, "timestamp": "2026-05-22T12:01:00+00:00"},
        {"auction_id": "3", "our_solution": {"id": 3, "trades": []}, "winner_solution": None, "timestamp": "2026-05-22T12:02:00+00:00"},
    ]
    log_path.write_text("\n".join(json.dumps(l) for l in lines))

    stats = analyze(log_path)
    assert stats["total_auctions"] == 3
    assert stats["our_attempts"] == 2  # 1 + 3 had our_solution non-None
    assert stats["winners_seen"] == 2  # 1 + 2 had winner
    assert stats["winner_counts"] == {"barter": 1, "rizzolver": 1}
```

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/test_shadow/test_analyzer.py -v`
Expected: FAIL.

- [ ] **Step 3: Create `src/shadow/analyzer.py`**

```python
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


def analyze(log_path: Path) -> dict[str, Any]:
    total = 0
    our_attempts = 0
    winners_seen = 0
    winner_counts: Counter[str] = Counter()

    with log_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            total += 1
            if rec.get("our_solution") is not None:
                our_attempts += 1
            w = rec.get("winner_solution")
            if w is not None:
                winners_seen += 1
                winner_counts[w["solver"]] += 1

    return {
        "total_auctions": total,
        "our_attempts": our_attempts,
        "winners_seen": winners_seen,
        "winner_counts": dict(winner_counts),
        "attempt_rate": our_attempts / total if total else 0.0,
    }


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m src.shadow.analyzer <log.jsonl>", file=sys.stderr)
        return 2
    stats = analyze(Path(sys.argv[1]))
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/test_shadow/test_analyzer.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/shadow/analyzer.py tests/test_shadow/test_analyzer.py
git commit -m "feat(shadow): analyzer CLI reports basic batch stats"
```

---

## Task 16: Background reconciliation worker — fill winner solutions

**Files:**
- Create: `src/shadow/reconcile.py`
- Create: `tests/test_shadow/test_reconcile.py`

This worker walks the JSONL file periodically, fetches missing `winner_solution` entries via CoW API, and rewrites the file.

- [ ] **Step 1: Write failing test**

`tests/test_shadow/test_reconcile.py`:
```python
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.shadow.cow_api import CompetitionResult
from src.shadow.reconcile import reconcile_once


async def test_reconcile_fills_winner(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    lines = [
        {"auction_id": "1", "our_solution": None, "winner_solution": None, "timestamp": "t1"},
        {"auction_id": "2", "our_solution": {"id": 2}, "winner_solution": None, "timestamp": "t2"},
        {"auction_id": "3", "our_solution": None, "winner_solution": {"solver": "x", "score": 1}, "timestamp": "t3"},
    ]
    log_path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")

    cow_api = AsyncMock()
    cow_api.fetch_competition.side_effect = [
        CompetitionResult(auction_id=1, winner_solver="barter", winner_score=100),
        CompetitionResult(auction_id=2, winner_solver="rizzolver", winner_score=200),
    ]

    updated = await reconcile_once(log_path, cow_api)
    assert updated == 2

    result_lines = [json.loads(l) for l in log_path.read_text().strip().splitlines()]
    assert result_lines[0]["winner_solution"] == {"solver": "barter", "score": 100}
    assert result_lines[1]["winner_solution"] == {"solver": "rizzolver", "score": 200}
    assert result_lines[2]["winner_solution"] == {"solver": "x", "score": 1}  # unchanged


async def test_reconcile_skips_missing_competition(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    log_path.write_text(json.dumps({"auction_id": "99", "our_solution": None, "winner_solution": None, "timestamp": "t"}) + "\n")
    cow_api = AsyncMock()
    cow_api.fetch_competition.return_value = None

    updated = await reconcile_once(log_path, cow_api)
    assert updated == 0
```

- [ ] **Step 2: Run, verify FAIL**

Run: `uv run pytest tests/test_shadow/test_reconcile.py -v`
Expected: FAIL.

- [ ] **Step 3: Create `src/shadow/reconcile.py`**

```python
import json
from pathlib import Path

from src.log import get_logger
from src.shadow.cow_api import CowApiClient

log = get_logger(__name__)


async def reconcile_once(log_path: Path, cow_api: CowApiClient) -> int:
    """Walks the JSONL file once, fills missing winner_solution entries.

    Returns the number of records updated.
    """
    if not log_path.exists():
        return 0

    records = [json.loads(line) for line in log_path.read_text().strip().splitlines() if line]
    updated = 0

    for rec in records:
        if rec.get("winner_solution") is not None:
            continue
        comp = await cow_api.fetch_competition(int(rec["auction_id"]))
        if comp is None:
            continue
        rec["winner_solution"] = {"solver": comp.winner_solver, "score": comp.winner_score}
        updated += 1

    if updated:
        # Rewrite atomically: write to temp then rename
        tmp = log_path.with_suffix(log_path.suffix + ".tmp")
        with tmp.open("w") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
        tmp.replace(log_path)

    log.info("reconcile_done", updated=updated, total=len(records))
    return updated
```

- [ ] **Step 4: Run, verify PASS**

Run: `uv run pytest tests/test_shadow/test_reconcile.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/shadow/reconcile.py tests/test_shadow/test_reconcile.py
git commit -m "feat(shadow): reconciliation worker fills winner solutions"
```

---

## Task 17: Reconciliation runner script

**Files:**
- Create: `scripts/reconcile_loop.py`

- [ ] **Step 1: Create `scripts/reconcile_loop.py`**

```python
"""Run reconciliation in a loop. Invoked by docker-compose as a sidecar service."""

import asyncio

from src.config import settings
from src.log import configure_logging, get_logger
from src.shadow.cow_api import CowApiClient
from src.shadow.reconcile import reconcile_once

log = get_logger(__name__)


async def main() -> None:
    configure_logging(level=settings.log_level)
    cow_api = CowApiClient(network="arbitrum_one")
    log_path = settings.shadow_log_path

    while True:
        try:
            updated = await reconcile_once(log_path, cow_api)
            log.info("reconcile_cycle", updated=updated)
        except Exception as e:  # noqa: BLE001
            log.error("reconcile_failed", error=str(e))
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Manual sanity check** (no test — just verify it imports cleanly)

Run: `uv run python -c "import scripts.reconcile_loop"`
Expected: no error. (Add `scripts/__init__.py` if needed.)

```bash
touch scripts/__init__.py
```

- [ ] **Step 3: Commit**

```bash
git add scripts/
git commit -m "feat(shadow): reconcile_loop script for docker sidecar"
```

---

## Task 18: Dockerfile (multi-stage)

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`

- [ ] **Step 1: Create `.dockerignore`**

```
.venv/
.git/
.pytest_cache/
.mypy_cache/
.ruff_cache/
shadow_data/
*.jsonl
tests/
docs/
.github/
.env
.env.local
```

- [ ] **Step 2: Create `Dockerfile`**

```dockerfile
# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5.4 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-install-project --no-dev || uv sync --no-install-project --no-dev

COPY src ./src
COPY scripts ./scripts
COPY edge ./edge
RUN uv sync --frozen --no-dev || uv sync --no-dev


FROM python:3.12-slim AS runtime

RUN useradd -m -u 1000 app && \
    mkdir -p /data && chown app:app /data

WORKDIR /app
COPY --from=builder --chown=app:app /app /app

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health').read()" || exit 1

CMD ["uvicorn", "--factory", "src.main:build_default_app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 3: Build locally**

Run:
```bash
docker build -t cow-solver:dev .
```
Expected: image builds successfully, no errors.

- [ ] **Step 4: Smoke test the container**

Run:
```bash
docker run --rm -d --name cow-solver-test -p 8000:8000 \
    -e ONEINCH_API_KEY=dummy cow-solver:dev
sleep 3
curl -s http://localhost:8000/health
docker stop cow-solver-test
```
Expected: `{"status":"ok"}`.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile .dockerignore
git commit -m "build: multi-stage Dockerfile with uv"
```

---

## Task 19: docker-compose.yml for local development

**Files:**
- Create: `docker-compose.yml`

- [ ] **Step 1: Create `docker-compose.yml`**

```yaml
services:
  cow-solver:
    build: .
    image: cow-solver:dev
    container_name: cow-solver
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      ONEINCH_API_KEY: ${ONEINCH_API_KEY:-dummy}
      RPC_ARBITRUM: ${RPC_ARBITRUM:-https://arb1.arbitrum.io/rpc}
      REDIS_URL: redis://redis:6379/3
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
      SHADOW_LOG_PATH: /data/shadow.jsonl
    volumes:
      - ./shadow_data:/data
    depends_on:
      redis:
        condition: service_healthy

  reconciler:
    build: .
    image: cow-solver:dev
    container_name: cow-reconciler
    restart: unless-stopped
    command: ["python", "-m", "scripts.reconcile_loop"]
    environment:
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
      SHADOW_LOG_PATH: /data/shadow.jsonl
    volumes:
      - ./shadow_data:/data

  redis:
    image: redis:7-alpine
    container_name: cow-solver-redis
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  # Shadow driver: only enabled when explicitly started.
  # Use: docker compose --profile shadow up
  shadow-driver:
    image: ghcr.io/cowprotocol/services:latest
    container_name: cow-shadow-driver
    profiles: ["shadow"]
    command: ["driver", "--config", "/etc/driver.shadow.toml", "--shadow"]
    volumes:
      - ./config/driver.shadow.toml:/etc/driver.shadow.toml:ro
    depends_on:
      cow-solver:
        condition: service_healthy
```

- [ ] **Step 2: Local smoke test**

Run:
```bash
docker compose up -d cow-solver redis
sleep 5
curl -s http://localhost:8000/health
docker compose down
```
Expected: `{"status":"ok"}`.

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "build: docker-compose for local dev with solver + redis + reconciler"
```

---

## Task 20: Shadow driver config

**Files:**
- Create: `config/driver.shadow.toml`

Driver config reference: https://github.com/cowprotocol/services/blob/main/crates/driver/example.toml

- [ ] **Step 1: Create `config/driver.shadow.toml`**

```toml
# Shadow-mode driver config for cow-solver-skeleton.
# Driver receives real Arbitrum batches from CoW autopilot but does NOT
# submit settlements on-chain.

[shadow]
# Run as observer
solvers = ["http://cow-solver:8000"]

[liquidity]
# Driver will index AMM state and provide it to our solver via the auction payload.
# Disabling some sources reduces driver memory; enable as needed.
[liquidity.uniswap-v2]
router = "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24"  # Uniswap V2 router on Arbitrum

[liquidity.uniswap-v3]
router = "0xE592427A0AEce92De3Edee1F18E0157C05861564"  # Uniswap V3 SwapRouter on Arbitrum

[submission]
# Shadow mode: no submission keys needed
gas-price-cap = "1000000000"  # 1 gwei cap (informational)

[contracts]
# Arbitrum addresses
gp-v2-settlement = "0x9008D19f58AAbD9eD0D60971565AA8510560ab41"

[ethereum]
node-url = "${RPC_ARBITRUM}"
```

NOTE: Verify these contract/router addresses against the latest driver config in the CoW services repo before deploying — the example above is a reasonable starting point but should match the active deployment.

- [ ] **Step 2: Commit**

```bash
git add config/driver.shadow.toml
git commit -m "build: shadow driver config for Arbitrum"
```

---

## Task 21: GitHub Actions CI workflow

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Create `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  lint-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: false  # edge stays private; CI runs without it

      - name: Install uv
        uses: astral-sh/setup-uv@v3
        with:
          version: "0.5.4"

      - name: Set up Python
        run: uv python install 3.12

      - name: Install dependencies
        run: uv sync --dev

      - name: Ruff lint
        run: uv run ruff check src tests

      - name: Mypy
        run: uv run mypy src

      - name: Pytest
        run: uv run pytest --cov=src --cov-report=term-missing
```

- [ ] **Step 2: Commit and push to verify workflow**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: lint, typecheck, and test workflow"
git push origin main
```

Then check: `gh run watch` or visit GitHub Actions tab. Expected: workflow succeeds on lint + tests.

If mypy fails: fix the offending types in the relevant files. Do not skip mypy.

---

## Task 22: GitHub Actions deploy workflow (build + push to GHCR)

**Files:**
- Create: `.github/workflows/deploy.yml`

This task creates the workflow file but **does not run** yet — Hetzner secrets are added in Task 26.

- [ ] **Step 1: Create `.github/workflows/deploy.yml`**

```yaml
name: Deploy

on:
  push:
    branches: [main]
  workflow_dispatch:

concurrency:
  group: deploy-${{ github.ref }}
  cancel-in-progress: false

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    outputs:
      image_tag: ${{ steps.meta.outputs.tags }}
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: recursive
          token: ${{ secrets.EDGE_SUBMODULE_PAT }}

      - uses: docker/setup-buildx-action@v3

      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Compute tags
        id: meta
        run: |
          echo "tags=ghcr.io/${{ github.repository_owner }}/cow-solver:latest,ghcr.io/${{ github.repository_owner }}/cow-solver:${{ github.sha }}" >> $GITHUB_OUTPUT

      - uses: docker/build-push-action@v5
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

  deploy:
    needs: build
    runs-on: ubuntu-latest
    if: ${{ vars.DEPLOY_ENABLED == 'true' }}
    steps:
      - uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.HETZNER_HOST }}
          username: ${{ secrets.HETZNER_USER }}
          key: ${{ secrets.HETZNER_SSH_KEY }}
          script: |
            set -e
            cd /opt/mhagentic/stack
            docker compose pull cow-solver reconciler
            docker compose up -d cow-solver reconciler
            sleep 5
            docker compose ps cow-solver
            curl -fsS http://localhost:8000/health
```

NOTE: The deploy job runs only when the repository variable `DEPLOY_ENABLED=true`. We set this in Task 26 after secrets are in place.

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/deploy.yml
git commit -m "ci: deploy workflow (gated on DEPLOY_ENABLED var)"
git push origin main
```

Expected: workflow appears in Actions tab. Build job runs (and likely succeeds publishing to GHCR — that doesn't need any extra secrets beyond `EDGE_SUBMODULE_PAT`, which can be empty for now since edge submodule is empty). Deploy job is skipped.

---

## Task 23: README.md and DEPLOYMENT.md

**Files:**
- Modify: `README.md`
- Create: `docs/DEPLOYMENT.md`

- [ ] **Step 1: Overwrite `README.md`**

```markdown
# cow-solver-skeleton

Public skeleton for a CoW Protocol solver competing on Arbitrum. The
competitive edge (CoW-matching + long-tail pool indexer) lives in a private
git submodule under `edge/` and is loaded at runtime if present.

See [design spec](docs/superpowers/specs/2026-05-22-cow-solver-design.md).

## Local development

```bash
uv sync
uv run pytest
uv run uvicorn --factory src.main:build_default_app --reload
```

## Local shadow test

```bash
# 1inch API key required
echo "ONEINCH_API_KEY=your-key" > .env
docker compose --profile shadow up -d
docker compose logs -f cow-solver
```

The shadow driver receives real Arbitrum batches and calls `cow-solver:8000/solve`.

## Deployment

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
```

- [ ] **Step 2: Create `docs/DEPLOYMENT.md`**

```markdown
# Deployment

## Target environment

- Hetzner CX22 at the shared mhagentic stack (`/opt/mhagentic/stack/`).
- AI Backoffice MUST be stopped before solver runs in shadow phase
  (RAM contention — see [design spec §4.1](superpowers/specs/2026-05-22-cow-solver-design.md)).

## Initial setup (one-time)

1. SSH to Hetzner.
2. Stop Backoffice:
   ```bash
   cd /opt/mhagentic/stack
   docker compose stop backoffice-api backoffice-worker ollama whisper grafana prometheus
   ```
3. Create solver dir and copy compose snippet (from `deploy/hetzner/`):
   ```bash
   mkdir -p /opt/mhagentic/stack/cow-solver/config
   # copy docker-compose.yml and config/driver.shadow.toml
   ```
4. Append `cow-solver/docker-compose.yml` to top-level `stack/docker-compose.yml` include section.
5. Set env vars in `cow-solver/.env`:
   ```
   ONEINCH_API_KEY=...
   RPC_ARBITRUM=https://arb1.arbitrum.io/rpc
   SOLVER_TAG=latest
   ```
6. Trigger first deploy: `git push origin main` (will use latest GHCR image).

## Monitoring

- `http://<server>:8001/metrics` — Prometheus scrape target
- Logs: `docker compose logs -f cow-solver`
- Shadow data: `/opt/mhagentic/stack/cow-solver/data/shadow.jsonl`

## Rollback

```bash
docker compose pull cow-solver
SOLVER_TAG=<previous-sha> docker compose up -d cow-solver
```
```

- [ ] **Step 3: Commit**

```bash
git add README.md docs/DEPLOYMENT.md
git commit -m "docs: README and DEPLOYMENT guide"
```

---

## Task 24: Set up edge submodule and deploy key

**REQUIRES USER**: P5 (generate and add deploy key).

**Files:**
- Modify: `.gitmodules`

- [ ] **Step 1: Initialize private edge repo with placeholder**

Run (in a temp clone of `cow-solver-edge`):
```bash
mkdir -p /tmp/cow-solver-edge && cd /tmp/cow-solver-edge
git init -b main
cat > README.md <<'EOF'
# cow-solver-edge

Private edge module for cow-solver-skeleton. Contains CoW-matching algorithm
and long-tail pool indexer. Loaded as a git submodule by the public skeleton.

Empty for Phase 0–1. Populated in Phase 2.
EOF
mkdir -p matching pool_indexer classifier
touch matching/__init__.py pool_indexer/__init__.py classifier/__init__.py
echo "# placeholder" > matching/README.md
echo "# placeholder" > pool_indexer/README.md
echo "# placeholder" > classifier/README.md
git add -A
git commit -m "chore: scaffold private edge submodule"
git remote add origin git@github.com:mounirhajri/cow-solver-edge.git
git push -u origin main
```

- [ ] **Step 2: Add submodule to skeleton**

```bash
cd /Users/mma/workspace/cow-solver-skeleton
rm -rf edge
git submodule add git@github.com:mounirhajri/cow-solver-edge.git edge
git commit -m "feat: add edge as private git submodule"
git push origin main
```

- [ ] **Step 3: Generate deploy key for GitHub Actions**

Run on local machine:
```bash
ssh-keygen -t ed25519 -N "" -C "cow-solver-edge-deploy" -f /tmp/edge_deploy_key
echo "--- PUBLIC KEY (add to cow-solver-edge → Settings → Deploy keys, read-only): ---"
cat /tmp/edge_deploy_key.pub
echo ""
echo "--- PRIVATE KEY (add to cow-solver-skeleton → Settings → Secrets → EDGE_SUBMODULE_KEY): ---"
cat /tmp/edge_deploy_key
```

**USER ACTION:**
1. Copy the **public key** to `cow-solver-edge` → Settings → Deploy keys → Add deploy key. Title: "skeleton-CI". Allow write access: NO.
2. Copy the **private key** to `cow-solver-skeleton` → Settings → Secrets and variables → Actions → New secret. Name: `EDGE_SUBMODULE_PAT` (or rename in workflow). For ssh-key based access, also configure `actions/checkout` to use ssh — adjust workflow accordingly:

```yaml
# Update .github/workflows/deploy.yml checkout step:
- uses: actions/checkout@v4
  with:
    submodules: recursive
    ssh-key: ${{ secrets.EDGE_SUBMODULE_PAT }}
```

3. Confirm to assistant when done.

- [ ] **Step 4: Commit workflow update**

```bash
git add .github/workflows/deploy.yml
git commit -m "ci: use ssh-key for private submodule checkout"
git push origin main
```

- [ ] **Step 5: Verify**

Run: `gh run watch`
Expected: deploy workflow's build job completes (still no deploy because DEPLOY_ENABLED still false).

- [ ] **Step 6: Cleanup**

```bash
rm /tmp/edge_deploy_key /tmp/edge_deploy_key.pub
```

---

## Task 25: Disk hygiene check on Hetzner

**REQUIRES USER**: SSH access to Hetzner.

- [ ] **Step 1: Verify disk usage**

User runs (or assistant via SSH if authorized):
```bash
ssh hetzner df -h /
ssh hetzner docker system df
```
Expected: `/` is below 85% before continuing. If above, run:

```bash
ssh hetzner docker image prune -af --filter "until=72h"
ssh hetzner journalctl --vacuum-time=7d
ssh hetzner docker system df
```

- [ ] **Step 2: Verify Backoffice is currently running (so we know what to stop later)**

```bash
ssh hetzner "cd /opt/mhagentic/stack && docker compose ps"
```
Expected: list of running containers, note which to stop in Task 27.

- [ ] **Step 3: Record current state**

Save output of `docker compose ps` somewhere — needed for restoring Backoffice after shadow phase ends.

---

## Task 26: Configure Hetzner secrets and enable deploy

**REQUIRES USER**: P6.

- [ ] **Step 1: Generate Hetzner SSH key for CI** (if not exists)

Run locally:
```bash
ssh-keygen -t ed25519 -N "" -C "cow-solver-ci" -f /tmp/hetzner_ci_key
ssh-copy-id -i /tmp/hetzner_ci_key.pub <hetzner-user>@<hetzner-host>
echo "--- Private key (add to GH secret HETZNER_SSH_KEY): ---"
cat /tmp/hetzner_ci_key
```

**USER ACTION:** Add to `cow-solver-skeleton` → Settings → Secrets:
- `HETZNER_HOST` = your hetzner IP / hostname
- `HETZNER_USER` = your hetzner SSH user
- `HETZNER_SSH_KEY` = contents of `/tmp/hetzner_ci_key`

Then add a repository **variable** (not secret):
- `DEPLOY_ENABLED` = `true`

- [ ] **Step 2: Cleanup local key**

```bash
rm /tmp/hetzner_ci_key /tmp/hetzner_ci_key.pub
```

- [ ] **Step 3: Confirm to assistant when complete.**

---

## Task 27: First Hetzner deploy (manual, before CI takes over)

**REQUIRES:** P7 (Backoffice stopped).

- [ ] **Step 1: Stop AI Backoffice**

```bash
ssh hetzner "cd /opt/mhagentic/stack && docker compose stop backoffice-api backoffice-worker backoffice-api-staging backoffice-worker-staging ollama whisper grafana prometheus"
```

- [ ] **Step 2: Create solver dir on server**

```bash
ssh hetzner "mkdir -p /opt/mhagentic/stack/cow-solver/config /opt/mhagentic/stack/cow-solver/data"
```

- [ ] **Step 3: Copy production compose snippet**

Create locally `deploy/hetzner/docker-compose.prod.yml`:

```yaml
services:
  cow-solver:
    image: ghcr.io/mounirhajri/cow-solver:${SOLVER_TAG:-latest}
    container_name: cow-solver
    restart: unless-stopped
    deploy:
      resources:
        limits:
          cpus: '1.8'
          memory: 3G
        reservations:
          memory: 512M
    environment:
      REDIS_URL: redis://redis:6379/3
      REDIS_KEY_PREFIX: "solver:"
      POOL_CACHE_MAX_ENTRIES: 800
      POOL_CACHE_TTL_SECONDS: 60
      AMM_STATE_LAZY: "true"
      RPC_ARBITRUM: ${RPC_ARBITRUM}
      ONEINCH_API_KEY: ${ONEINCH_API_KEY}
      SHADOW_LOG_PATH: /data/shadow.jsonl
      LOG_LEVEL: INFO
      PROMETHEUS_PORT: 8001
    volumes:
      - ./data:/data
    networks: [stack_default]
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]
      interval: 30s
      timeout: 5s
      retries: 3

  reconciler:
    image: ghcr.io/mounirhajri/cow-solver:${SOLVER_TAG:-latest}
    container_name: cow-reconciler
    restart: unless-stopped
    command: ["python", "-m", "scripts.reconcile_loop"]
    deploy:
      resources:
        limits:
          memory: 256M
    environment:
      LOG_LEVEL: INFO
      SHADOW_LOG_PATH: /data/shadow.jsonl
    volumes:
      - ./data:/data
    networks: [stack_default]

  cow-shadow-driver:
    image: ghcr.io/cowprotocol/services:latest
    container_name: cow-shadow-driver
    restart: unless-stopped
    command: ["driver", "--config", "/etc/driver.shadow.toml", "--shadow"]
    deploy:
      resources:
        limits:
          memory: 512M
    volumes:
      - ./config/driver.shadow.toml:/etc/driver.shadow.toml:ro
    depends_on:
      cow-solver:
        condition: service_healthy
    networks: [stack_default]

networks:
  stack_default:
    external: true
```

Copy to server:
```bash
scp deploy/hetzner/docker-compose.prod.yml hetzner:/opt/mhagentic/stack/cow-solver/docker-compose.yml
scp config/driver.shadow.toml hetzner:/opt/mhagentic/stack/cow-solver/config/
```

- [ ] **Step 4: Create solver `.env` on server**

```bash
ssh hetzner "cat > /opt/mhagentic/stack/cow-solver/.env <<EOF
ONEINCH_API_KEY=<your-key>
RPC_ARBITRUM=https://arb1.arbitrum.io/rpc
SOLVER_TAG=latest
EOF"
```

- [ ] **Step 5: Add include to top-level stack compose**

```bash
ssh hetzner "cd /opt/mhagentic/stack && cp docker-compose.yml docker-compose.yml.bak"
# Then edit to add `- cow-solver/docker-compose.yml` to the `include:` list
```

USER ACTION: Edit the file (vim/nano) to add the new include line. Verify with:
```bash
ssh hetzner "cd /opt/mhagentic/stack && docker compose config | grep cow-solver"
```

- [ ] **Step 6: Pull and start**

```bash
ssh hetzner "cd /opt/mhagentic/stack && docker compose pull cow-solver reconciler cow-shadow-driver"
ssh hetzner "cd /opt/mhagentic/stack && docker compose up -d cow-solver reconciler"
sleep 10
ssh hetzner "cd /opt/mhagentic/stack && docker compose ps cow-solver"
ssh hetzner "curl -fsS http://localhost:8000/health"
```
Expected: `{"status":"ok"}` and container healthy.

- [ ] **Step 7: Start shadow driver**

```bash
ssh hetzner "cd /opt/mhagentic/stack && docker compose up -d cow-shadow-driver"
ssh hetzner "docker logs -f cow-shadow-driver --tail 50"
```
Expected: Driver connects to CoW autopilot, starts streaming auctions. You should see lines like "received auction id=...".

- [ ] **Step 8: Verify /solve is being called**

In a new terminal:
```bash
ssh hetzner "docker logs -f cow-solver --tail 50"
```
Expected: structured JSON log lines with "auction_id" entries, increasing over time.

- [ ] **Step 9: Verify shadow.jsonl is growing**

After 10 minutes:
```bash
ssh hetzner "wc -l /opt/mhagentic/stack/cow-solver/data/shadow.jsonl"
ssh hetzner "tail -3 /opt/mhagentic/stack/cow-solver/data/shadow.jsonl"
```
Expected: 20+ lines (CoW auctions every ~30s); last lines have `winner_solution` populated by reconciler.

---

## Task 28: Phase 1 exit gate — collect 500 batches and run analyzer

- [ ] **Step 1: Wait until at least 500 auction lines are in JSONL**

```bash
ssh hetzner "wc -l /opt/mhagentic/stack/cow-solver/data/shadow.jsonl"
```
At ~30s/auction, 500 lines takes ~4 hours. Practically, leave it running ~24h for buffer.

- [ ] **Step 2: Pull analyzer locally and run**

```bash
scp hetzner:/opt/mhagentic/stack/cow-solver/data/shadow.jsonl ./shadow_data/log.jsonl
uv run python -m src.shadow.analyzer ./shadow_data/log.jsonl
```
Expected: JSON output like:
```json
{
  "total_auctions": 547,
  "our_attempts": 312,
  "winners_seen": 491,
  "winner_counts": {"barter": 132, "rizzolver": 121, ...},
  "attempt_rate": 0.57
}
```

- [ ] **Step 3: Sanity-check the data**

- `attempt_rate` should be >40%. If much lower, NaiveSolver is failing most auctions — investigate (likely 1inch quote failures or below-limit prices).
- `winner_counts` should match the public Dune dashboard ballpark.

If both look reasonable: **Phase 1 exit gate cleared**. Proceed to Plan 2 (Phase 2 edge implementation).

If anomalies: debug before proceeding. Phase 2 builds on shadow data; bad data = wasted Phase 2 effort.

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "chore: phase 1 exit — shadow data collection verified"
git push origin main
```

---

## Plan complete

After Task 28 you have:
- Public skeleton repo with full FastAPI solver, shadow runner, analyzer
- Private edge repo scaffolded (empty matching/pool_indexer placeholders)
- Hetzner deployment via push-to-main GitHub Actions
- 24+ hours of real Arbitrum shadow data
- Analyzer baseline showing where the naive solver stands

**Next plan:** Phase 2 — CoW-matching algorithm + long-tail pool indexer. Written separately because its design will be informed by Phase 1 data (which pairs/sizes we lose on, which winning solvers we'd need to beat).

---

## Self-review notes

- All tasks have exact file paths, complete code, and verifiable commands.
- TDD discipline maintained: every code-producing task has Step 1 (write failing test) before Step 3 (implementation).
- No placeholders. Every code block compiles.
- Type signatures consistent: `SolverStrategy.solve(auction: Auction) -> Solution | NoSolution` matches all implementations.
- File structure aligned with design spec §3.2.
- Phase 0 (Setup) = Tasks 1–11. Phase 1 (Shadow Naive) = Tasks 12–28.
- User actions (P1–P7) are listed up front and re-flagged in their respective tasks.
- Hetzner deployment respects Option C constraint: backoffice stopped during shadow.
- Exit gate (Task 28) is quantitative: ≥500 auctions, ≥40% attempt rate.
