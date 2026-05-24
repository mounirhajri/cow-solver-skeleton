# cow-solver-skeleton

A [CoW Protocol](https://cow.fi) solver for **Arbitrum One** implementing
multi-party Coincidence-of-Wants matching via Johnson's elementary-circuit
enumeration over a token-quantity LP, alongside conventional AMM routing.

The public skeleton hosts orchestration, AMM routing, persistence, and
the shadow harness. The matching algorithms + token-quality classifier
live in a private git submodule under `edge/`, loaded at runtime when
present. Public clones fall back to the naive + router strategies only.

## Strategy chain

The orchestrator runs strategies in this order, with a CIP-67 composer
merging non-conflicting solutions:

| # | Strategy | Module | Purpose |
|---|----------|--------|---------|
| 1 | NaiveSolver | `src/solver/naive.py` | 1inch wrapper with on-chain price refinement (Multicall3 + V2/V3) |
| 2 | BipartiteMatcher | `edge/matching/bipartite.py` | Two-party CoW direct matching |
| 3 | CoWMatchingSolver | `edge/matching/multi_party.py` | Johnson cycle finder (rustworkx) + quantity-space LP (scipy HiGHS) for 3-4 party rings |
| 4 | LongTailRouter | `edge/pool_indexer/long_tail_router.py` | UniV2-style routing via Redis-cached pool index (gated, off by default in prod) |
| 5 | RouterSolver | `src/solver/router.py` | Top-N orders by ETH-value × V3 QuoterV2 across 4 fee tiers, batched in one Multicall3 round-trip |

Composer (`edge/matching/composer.py`) enforces uniform clearing prices
per directed token pair (±2 % tolerance) when merging multiple winners.

## Design rationale

Each phase has a written spec in `docs/superpowers/specs/`:

- [`2026-05-22-cow-solver-design.md`](docs/superpowers/specs/2026-05-22-cow-solver-design.md)
  — high-level architecture, phasing, risks
- [`2026-05-23-solver-revenue-strategy-design.md`](docs/superpowers/specs/2026-05-23-solver-revenue-strategy-design.md)
  — tactical revenue plan, G1-G6 go/no-go gate, current status per phase
- [`2026-05-23-phase2-pool-indexer-design.md`](docs/superpowers/specs/2026-05-23-phase2-pool-indexer-design.md)
  — Pool-Indexer MVP design (lazy indexer, Redis cache)

## Local development

```bash
uv sync
uv run pytest          # ~290 tests
uv run uvicorn --factory src.main:build_default_app --reload
```

Tests cover routing math, matching algorithms (bipartite + multi-party
LP), composer, shadow scoring (CIP-14), and persistence.

## Shadow run (local)

```bash
echo "ONEINCH_API_KEY=your-key" > .env
docker compose --profile shadow up -d
docker compose logs -f cow-solver
```

The shadow driver receives real Arbitrum batches from the public CoW
poller and calls `cow-solver:8000/solve`. Solutions are persisted to
Postgres for later analysis via the scripts in `scripts/`.

## Analysis tooling

After accumulating shadow data:

```bash
# Per-strategy hypothetical win-rate over a window
docker exec cow-solver python -m scripts.analyze_router_solutions --days 7
docker exec cow-solver python -m scripts.analyze_cow_rings --days 7

# Break-even economics projection (G6 gate)
docker exec cow-solver python -m scripts.estimate_economics --days 7 \
  --eth-price-eur 3000 --cow-price-eur 0.15 --server-cost-eur 60

# Backfill CIP-14 scores for older rows
docker exec cow-solver python -m scripts.backfill_scores
```

## Deployment

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). Push to `main` triggers a
GitHub Actions deploy via SSH to the production host. Alembic migrations
run automatically on container restart.

## Configuration knobs

Environment variables (see `src/config.py` for full list + defaults):

| Var | Default | Effect |
|-----|---------|--------|
| `RPC_ARBITRUM` | public RPC | Arbitrum JSON-RPC endpoint |
| `ROUTER_V3_ONLY_BATCHED` | `true` | RouterSolver uses V3-batched mode (1 RPC/auction); set false for legacy V2+V3 fan-out |
| `LONG_TAIL_ENABLED` | `true` | Set `false` to disable LongTailRouter (recommended on tight RPC tiers) |
| `MULTI_PARTY_OTM_TOLERANCE_BPS` | `100` | Widens ring-candidate graph beyond strict reference-price-ITM (0 = legacy strict) |

## Edge submodule

The competitive matching algorithms + token-quality classifier are not
in this repository. Read-access to `cow-solver-edge` is granted on
request for code review. The public skeleton functions standalone (just
the naive + router strategies) when the submodule is absent.

## License

Skeleton: MIT. The edge submodule is proprietary.
