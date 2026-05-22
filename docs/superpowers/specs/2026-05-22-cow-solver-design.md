# CoW Protocol Solver — Design Spec

**Author:** mhajri
**Date:** 2026-05-22
**Status:** Draft — awaiting user review
**Project goal:** Build, deploy, and operate a competitive CoW Protocol solver on Arbitrum, hosted on existing Hetzner infrastructure, with the goal of earning weekly COW rewards.

---

## 1. Goals & Non-Goals

### Goals

- Build a production-grade CoW Protocol solver in Python, deployable to Hetzner
- Compete on Arbitrum (mandatory starting chain per CoW onboarding docs)
- Develop a defensible competitive edge as solo developer:
  - **Primary edge:** CoW-Matching maximalism (multi-party bipartite matching)
  - **Secondary edge:** Long-tail fresh-pool indexing (with constraints, see §6)
- Pass through Shadow → Onboarding (KYC) → Barn → Production phases
- Open-source the skeleton; keep the edge algorithm private

### Non-Goals

- Multi-chain expansion in v1 (Arbitrum only; later: Base, Gnosis)
- Own bonding mechanism (use CoW DAO bonding pool; 15% service fee after 6mo grace, 25% locked — subject to pending CIP draft from 2026-05-16)
- Submission key management (CoW Team manages this post-onboarding)
- MEV protection layer (CoW Protocol handles this natively)
- Custom Uniswap V3 math implementation (use ported SDK)
- Cross-chain bridge routing

---

## 2. Competitive Landscape (verified 2026-05-22)

| Fact | Source |
|------|--------|
| Active solvers on Ethereum: ~16 visible, 38 historical entries → ~22 quit | Dune `cowprotocol/solver-info` |
| Top solver (Rizzolver) market share: ~25%, next 5 each ~10% | Public Dune dashboards |
| Solver onboarding mandates starting on Arbitrum | docs.cow.fi/cow-protocol/tutorials/solvers/onboard |
| Shadow mode runs on Arbitrum + Mainnet | Same source |
| Current fee (CoW DAO bonding pool): 0% for 6mo, then 15% | CIP-48 |
| **Proposed change (May 2026, not yet approved):** 10% from day 1, 20% HODL rule | forum.cow.fi/t/cip-draft-simplifying...3455 |
| Minimum rewards per batch: 0.00024 ETH or 6 COW (Arbitrum) | docs.cow.fi |
| KYC: incorporation details + 1–2 passports of shareholders/devs | docs.cow.fi |
| Official Python template available | github.com/cowprotocol/solver-template-py |

**Strategic implication:** Distribution is not winner-take-all. A new solver capturing 2–5% share is meaningful. But 22 solvers have given up — execution risk is real.

---

## 3. Architecture

### 3.1 Overview

```
GitHub
├─ cow-solver-skeleton (public)
│  ├─ FastAPI /solve endpoint
│  ├─ Driver integration (shadow + staging configs)
│  ├─ Naive baseline solver (1inch wrapper)
│  ├─ Shadow runner & analyzer
│  └─ Tests, CI, Docker
└─ cow-solver-edge (private, submoduled under skeleton/edge/)
   ├─ Matching algorithm (bipartite + multi-party rings)
   ├─ Long-tail pool indexer (factory watcher, pool cache)
   └─ LLM token classifier

         │ GitHub Actions (build + push to GHCR)
         ▼
Hetzner CX22 (existing server, shared with AI Backoffice)
├─ Shadow phase: AI Backoffice stopped, solver gets full resources
├─ Production phase: decision point — upgrade to CX32 or split to own VM
└─ Solver container with hard resource limits

         │ HTTPS (Shadow: local; Production: CoW-managed driver calls)
         ▼
CoW Driver (Rust, managed by CoW Team after onboarding)
         │
         ▼
CoW Autopilot (CoW infrastructure)
```

### 3.2 Repo structure

**Public: `cow-solver-skeleton`**
```
cow-solver-skeleton/
├── README.md
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── .github/workflows/{ci,deploy}.yml
├── config/
│   ├── driver.shadow.toml
│   └── driver.staging.toml
├── src/
│   ├── main.py                # FastAPI app
│   ├── solver/
│   │   ├── base.py            # SolverStrategy Protocol
│   │   ├── naive.py           # 1inch baseline (public)
│   │   └── orchestrator.py    # Strategy chain
│   ├── routing/
│   │   ├── oneinch.py
│   │   └── rpc.py
│   ├── shadow/
│   │   ├── runner.py
│   │   └── analyzer.py
│   └── config.py
├── edge/                      # ← git submodule (private)
├── tests/
└── docs/superpowers/specs/
```

**Private submodule: `cow-solver-edge`**
```
cow-solver-edge/
├── matching/
│   ├── bipartite.py           # 2-party CoW
│   ├── multi_party.py         # 3+ party rings (Johnson's, depth=4)
│   └── surplus.py             # LP-based surplus maximization
├── pool_indexer/
│   ├── factory_watcher.py     # WebSocket on UniV2/V3, Camelot factories
│   ├── pool_cache.py          # Redis with key prefix solver:pool:*
│   └── liquidity_scorer.py
├── classifier/
│   └── token_llm.py           # Groq-backed token tagging
└── tests/
```

### 3.3 Public/Private interface

```python
# Public src/solver/orchestrator.py
from src.solver.naive import NaiveSolver

try:
    from edge.matching import CoWMatchingSolver
    from edge.pool_indexer import LongTailRouter
    STRATEGIES = [CoWMatchingSolver(), LongTailRouter(), NaiveSolver()]
except ImportError:
    STRATEGIES = [NaiveSolver()]  # public clone fallback
```

Public clones run a working baseline solver. The edge is only active when the private submodule is present (CI deploy key, or local clone with access).

### 3.4 Solver engine — per-batch flow

```
Auction in (10–50 orders, 15s budget)
  │
  ▼
[1] CoW-Matching (primary edge)
  • Build directed token graph from orders
  • Find cycles (Johnson's, bounded depth=4)
  • Per cycle: LP-solve for optimal volume distribution
  • Output: matched orders + residuals
  │
  ▼
[2] Long-Tail Routing (secondary edge, capped — see §4.1 for sizing)
  • For residuals: check indexed pools (LRU cache, size varies by phase)
  • Pool-cache lookup (Redis, <100ms)
  • Route via Uniswap-style direct swap if better than 1inch
  │
  ▼
[3] Naive Fallback (public baseline)
  • 1inch quote for remaining residuals
  • Standard AMM routing
  │
  ▼
Composer: uniform clearing prices per directed token pair (CIP-67)
  │
  ▼
Solution to driver
```

### 3.5 Latency budget (target)

| Stage | Budget | Notes |
|-------|--------|-------|
| Auction parse | 50ms | trivial |
| CoW-Matching (cycle find + LP) | 2s | depth=4, up to 50 orders |
| Long-Tail lookup | 1s | from cache |
| Naive 1inch fallback | 3s | external API |
| Composing + encoding | 500ms | |
| **Headroom** | **8s** | spike buffer |
| **Total** | **15s** | hard limit |

---

## 4. Deployment

### 4.1 Hetzner CX22 — Option C (lean shared)

**Pool-cache sizing per phase:**

| Phase | Backoffice | Solver CPU | Solver RAM | Pool cache |
|-------|-----------|------------|------------|------------|
| 1–2 (Shadow) | stopped | 1.8 | 3 GB | 800 entries |
| 3+ on CX22 (worst case) | running | 1.0 | 1.2 GB | 200 entries |
| 3+ on CX32 (option A) | running | 2.5 | 4 GB | 800+ entries |
| 3+ on own VM (option B) | n/a | full | full | 2000+ entries |

**Phase 3 decision point:** Must choose A or B before going to Staging/Barn, because production-grade solver must run 24/7 alongside the SaaS. Decision is deferred until Phase 2 produces RAM/CPU profiling data.

### 4.2 Compose integration

```yaml
# /opt/mhagentic/stack/docker-compose.yml — include
include:
  - ai-backoffice/docker-compose.yml
  - cow-solver/docker-compose.yml   # NEW
```

```yaml
# /opt/mhagentic/stack/cow-solver/docker-compose.yml
services:
  cow-solver:
    image: ghcr.io/mounirhajri/cow-solver:${SOLVER_TAG:-latest}
    container_name: cow-solver
    restart: unless-stopped
    deploy:
      resources:
        limits:
          cpus: '1.8'           # shadow phase
          memory: 3G            # shadow phase
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
      LOG_LEVEL: INFO
      PROMETHEUS_PORT: 8001
    networks: [stack_default]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s

  cow-solver-shadow:                # phase 1–2 only
    image: ghcr.io/cowprotocol/services:latest
    command: ["driver", "--config", "/etc/driver.shadow.toml", "--shadow"]
    deploy:
      resources:
        limits:
          memory: 512M
    volumes:
      - ./config/driver.shadow.toml:/etc/driver.shadow.toml:ro
    depends_on:
      cow-solver: { condition: service_healthy }
    networks: [stack_default]

networks:
  stack_default:
    external: true
```

### 4.3 CI/CD — push-to-deploy

```yaml
# .github/workflows/deploy.yml
on: { push: { branches: [main] } }
jobs:
  build:
    steps:
      - uses: actions/checkout@v4
        with: { submodules: recursive, token: ${{ secrets.EDGE_SUBMODULE_PAT }} }
      - uses: docker/login-action@v3
        with: { registry: ghcr.io, username: ${{ github.actor }}, password: ${{ secrets.GITHUB_TOKEN }} }
      - uses: docker/build-push-action@v5
        with:
          push: true
          tags: ghcr.io/mounirhajri/cow-solver:latest,ghcr.io/mounirhajri/cow-solver:${{ github.sha }}
  deploy:
    needs: build
    steps:
      - uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.HETZNER_HOST }}
          username: ${{ secrets.HETZNER_USER }}
          key: ${{ secrets.HETZNER_SSH_KEY }}
          script: |
            cd /opt/mhagentic/stack
            docker compose pull cow-solver
            docker compose up -d cow-solver
```

### 4.4 Monitoring (re-use existing infra)

- Solver exports Prometheus metrics on `:8001/metrics`
- Add scrape target to existing `prometheus.yml`: `cow-solver:8001`
- New Grafana dashboard `cow_solver.json`: win rate, surplus/batch, latency histograms, RAM usage
- Re-use existing Sentry DSN, separate project tag

### 4.5 Disk hygiene (precondition)

Server is at 91% disk per 2026-05-16. Before first deploy:
```bash
docker image prune -af --filter "until=72h"
docker system df
journalctl --vacuum-time=7d
```

---

## 5. Phasing & milestones

| Phase | Duration | Deliverable | Exit gate |
|-------|----------|-------------|-----------|
| **0 Setup** | 1 week | Repos created, skeleton scaffolded, local docker-compose works, /solve returns dummy solution | Naive solver responds correctly on simulated batch |
| **1 Shadow Naive** | 1–2 weeks | Shadow driver running on Hetzner against Arbitrum orderflow; naive solver = 1inch wrapper; logging both your solutions and winning solutions | 500+ shadow batches collected, analyzer reports surplus deltas |
| **2 Edge implementation** | 3–4 weeks | CoW-Matching v1 (2-party) → v2 (3+ rings); pool indexer v1 (Camelot + UniV2/V3 on Arbitrum); token classifier; iterative shadow tuning | **Hypothetical win-rate >5%**: in >5% of analyzed shadow batches, your solver's surplus exceeds the actual winning solver's surplus |
| **3 Onboarding prep** | 1 week | KYC docs prepared, server decision (A or B), rewards wallet, Telegram contact with CoW Team, code-review prep | Onboarding call completed |
| **4 Staging (Barn)** | 2–4 weeks | CoW Team sets up production driver, solver endpoint exposed via `solver.mhagenticworld.de`, first on-chain settlements in Barn | 50+ Barn settlements without errors |
| **5 Production** | ongoing | Weekly COW rewards (Tuesdays), continuous monitoring, edge iteration | n/a |

**Estimated effort:** Solo, ~10–15h/week → 3–4 months to production. Vollzeit → 6–8 weeks.

**Stop conditions:**
- After Phase 2: if hypothetical win-rate <2%, edge is too weak — rethink approach before investing in KYC
- After Phase 4: if real win-rate <1% despite correct settlements, edge is not competitive — KYC investment is sunk learning cost

---

## 6. Risks & open decisions

| Risk | Mitigation |
|------|------------|
| 22 solvers have quit historically — execution risk | Strict stop conditions (Phase 2/4); honest assessment before each phase transition |
| CX22 shared with AI Backoffice — RAM contention | Shadow phase: stop Backoffice. Production phase: deferred decision (upgrade vs split) |
| Pool indexer cache shrinks to 200 entries in worst-case production (CX22 + Backoffice running) → weakens long-tail edge by ~70% | CoW-Matching is primary edge; long-tail is opportunistic. Phase 3 decision (A/B) can restore cache size. |
| KYC requires passport + incorporation; CoW Team gets read-access to private edge repo | Acceptable — they review for security, not to steal algorithms |
| CIP-draft (2026-05-16) may change fee economics from 0%/6mo+15% to 10% day 1 + 20% HODL | Track proposal; fee economics affect break-even but not architecture |
| 15s latency budget tight if RPC slow on Arbitrum | Lazy AMM state, aggressive caching, dedicated RPC provider before Phase 4 |
| Disk at 91% on Hetzner pre-deploy | Prune images/logs before first deploy; monitor disk continuously |
| Solver-template-py may lag behind protocol changes (CIP-67 Fair Combinatorial) | Track protocol changelog, test against latest driver versions |

**Open decisions (need to be made during the project):**

1. **Server topology (Phase 3 decision):** A (upgrade CX32) vs B (own VM). Decide based on Phase 2 profiling.
2. **RPC provider:** public arb1.arbitrum.io for shadow, then Alchemy/Infura paid tier for production. Decide before Phase 4.
3. **Multi-chain expansion:** Base and Gnosis after Production. Not in v1 scope.
4. **Bonding pool vs own bond:** start with DAO pool. Re-evaluate after 6mo when fees activate.

---

## 7. Success criteria

**Phase 2 (shadow learning) success:**
- Solver runs 24/7 for 2 weeks without crashes
- Analyzer reports per-batch surplus delta vs winner
- Hypothetical win-rate ≥5% on Arbitrum

**Phase 4 (Barn) success:**
- 50+ on-chain settlements without errors
- Latency p95 < 12s
- No OOM kills

**Phase 5 (production) success:**
- Weekly COW rewards > server costs (CX32 €15/mo + RPC ~€20/mo)
- Win-rate ≥1% sustained over 30 days
- No disruption to AI Backoffice operations

---

## 8. What the user must do (cannot be automated)

| # | Task | Phase |
|---|------|-------|
| 1 | Sign up for 1inch API key at portal.1inch.dev | Pre-Phase 1 |
| 2 | Choose & set up Arbitrum RPC URL (public OK to start) | Pre-Phase 1 |
| 3 | Add GitHub deploy key for private submodule access | Phase 0 end |
| 4 | Add Hetzner secrets to GitHub Actions secrets | Pre-first-deploy |
| 5 | Stop AI Backoffice containers on Hetzner | Pre-Phase 1 deploy |
| 6 | Telegram contact with CoW Team (t.me/cowprotocol) | Phase 3 |
| 7 | Provide KYC documents (passport, optional Gewerbenachweis) | Phase 3 |
| 8 | Create rewards wallet (fresh MetaMask) | Phase 3 |
| 9 | Review & approve major code changes / risky deployments | Ongoing |

Everything else is automatable.

---

## 9. Sources

- [CoW Protocol Solver docs](https://docs.cow.fi/cow-protocol/concepts/introduction/solvers)
- [Solver Onboarding](https://docs.cow.fi/cow-protocol/tutorials/solvers/onboard)
- [Solver Info Dune](https://dune.com/cowprotocol/solver-info)
- [Coincidence of Wants](https://docs.cow.fi/cow-protocol/concepts/how-it-works/coincidence-of-wants)
- [Fair Combinatorial Auction (CIP-67)](https://docs.cow.fi/cow-protocol/concepts/introduction/fair-combinatorial-auction)
- [CIP-Draft: Bonding pool simplification, 2026-05-16](https://forum.cow.fi/t/cip-draft-simplifying-the-operations-of-the-cow-dao-bonding-pool/3455)
- [solver-template-py](https://github.com/cowprotocol/solver-template-py)
- [Local testing solvers](https://docs.cow.fi/cow-protocol/tutorials/solvers/local_test)
- [Solver rewards](https://docs.cow.fi/cow-protocol/reference/core/auctions/rewards)
- [Solver competition rules](https://docs.cow.fi/cow-protocol/reference/core/auctions/competition-rules)
