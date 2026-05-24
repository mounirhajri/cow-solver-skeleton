# Architecture

This document gives a one-page overview for readers (including the CoW
team during code review). For deeper rationale see the specs in
`docs/superpowers/specs/`.

## Request flow

```
CoW Driver  ──POST /solve──▶  FastAPI app  ──▶  SolverOrchestrator
                                                    │
                                ┌───────────────────┼───────────────────┐
                                ▼                   ▼                   ▼
                            Strategy 1          Strategy N          (all run in parallel,
                            NaiveSolver         RouterSolver         each with own timeout)
                                │                   │
                                └─────────┬─────────┘
                                          ▼
                                  CIP-67 Composer
                                   (token-disjoint,
                                    ±2% price tolerance)
                                          │
                                          ▼
                                  Solution + attempts
                                          │
                          ┌───────────────┼───────────────┐
                          ▼                               ▼
                  Driver response               Postgres (shadow_solutions,
                                                          shadow_winners,
                                                          shadow_auctions)
```

The orchestrator (`src/solver/orchestrator.py`) runs all strategies even
after the first non-empty solution, so the shadow data captures
per-strategy attempts for later analysis. Each strategy declares its own
timeout (RouterSolver: 11 s; default 5 s).

## Strategy chain (in order)

### 1. NaiveSolver (`src/solver/naive.py`)
Wraps 1inch quotes for individual orders. Then `price_refiner` issues
Multicall3-batched on-chain quotes (V2 + V3 via `routing/multihop.py`)
to replace oracle reference prices with real DEX-execution prices in the
final clearing-price vector. This is the always-on baseline.

### 2. BipartiteMatcher (`edge/matching/bipartite.py`)
Groups sell orders by directed token pair. For each pair `(A→B, B→A)`,
greedy-by-largest finds matchable pairs whose limits overlap. Emits 2
trades + midpoint clearing prices. Cheap and selective.

### 3. CoWMatchingSolver (`edge/matching/multi_party.py`)
Multi-party CoW via Johnson's elementary-circuit enumeration over the
directed token graph. The graph is built from in-the-money (or
OTM-tolerant) sell orders. Rings of length 3-4 are enumerated via
`rustworkx.simple_cycles`. For each ring, `edge/matching/surplus.py`
solves a quantity-space LP (scipy HiGHS):

- **Variables**: `x_i` (executed sell amount per order)
- **Constraints**: `s_i × x_{i+1} ≥ b_i × x_i` (cyclic ring-balance);
  `0 ≤ x_i ≤ s_i`
- **Feasibility**: ring is feasible iff `∏(b_i/s_i) ≤ 1`
  (reference-price-independent, pure user-limit math)
- **Objective**: maximize `Σ (x_{i+1} − b_i·x_i/s_i) × native_price(buy_i)`
  (reference prices used **only** as weights, not as constraints)
- **Clearing prices**: derived ring-internally from the LP's achieved
  per-hop ratios, anchored on `ring[0].sell_token`'s reference price

The composer in `multi_party.py` then greedy-picks non-overlapping rings
(token-disjoint to keep CIP-67 trivially satisfied across rings).

### 4. LongTailRouter (`edge/pool_indexer/long_tail_router.py`)
Lazy-indexed UniV2-style router. Per order, looks up cached pool
addresses (Sushi/Camelot V2) for the sell→buy pair via a Redis-backed
`PoolCache` (`solver:pool:addr:*` keys, 7d TTL for addresses, 60 s for
reserves). Quotes via `routing/amm_v2.py` constant-product math.
Gated by `LONG_TAIL_ENABLED` env var (disabled in prod when on a tight
RPC tier — its concurrent multicalls compete with RouterSolver for the
provider's connection budget).

### 5. RouterSolver (`src/solver/router.py`)
For top-N sell orders by ETH-equivalent value, requests V3 QuoterV2
quotes across 4 fee tiers (direct + 2-hop via configured intermediates)
and picks the best per order. Two modes via `ROUTER_V3_ONLY_BATCHED`:

- **Batched** (default, `true`): all candidate paths for the whole
  auction are batched into one Multicall3 round-trip (`routing/v3_batched.py`).
  ~80× RPC reduction vs the legacy per-order fan-out
- **Legacy**: per-order `asyncio.gather` over `quote_best_path`
  (kept for the public clone case where V3 quoters might be unavailable)

## CIP-67 composer (`edge/matching/composer.py`)

When multiple strategies return non-empty solutions, the composer
merges them subject to:

- **Per-order disjointness**: an order can appear in at most one trade
- **Per-token uniform clearing price**: if two strategies claim the same
  token at different prices, accept only if within ±2 % tolerance;
  otherwise drop the conflicting trade

The Multi-Party solver additionally enforces *ring-level* token
disjointness (see `multi_party.py` composer block) because each ring
derives its own anchor-relative clearing prices.

## Persistence

PostgreSQL via async SQLAlchemy. Schema (`src/persistence/models.py`):

- `shadow_auctions` — every auction we polled
- `shadow_solutions` — one row per strategy attempt per auction, with
  CIP-14 score (`our_score_wei`) and a winner-clearing-prices alternate
  score (`score_vs_winner_prices_wei`, populated when winner exposes
  `clearingPrices` — currently no-op on Arbitrum)
- `shadow_winners` — winner solution per auction (from CoW API)
- `token_outcomes` — per-token outcome label for the classifier
- `token_features` — token feature snapshot

Migrations via Alembic. Run automatically on container restart.

## Shadow → Barn → Production

1. **Shadow** (current): poll real Arbitrum auctions, call our `/solve`,
   persist + analyze. No on-chain settlement. G1-G6 gate measures
   readiness.
2. **Barn**: post-KYC, CoW Team registers our solver. Driver routes a
   subset of auctions to us; we settle real trades. Bonding pool covers
   collateral.
3. **Production**: full traffic share, weekly COW rewards.

## Key invariants and safety

- Strategies are isolated (per-strategy timeout + try/except in orchestrator)
- BackgroundTask persistence is never-raising (`persist_*_safe` wrappers)
- The edge submodule is optional at runtime; missing submodule degrades
  gracefully (only naive + router-v2 run)
- All clearing-price logic respects user limits at the prices we claim
  (LP enforces `s_i × x_{i+1} ≥ b_i × x_i` in atoms; clearing prices are
  derived such that the rate equals or exceeds each user's limit rate)
- RPC client retries 429 / -32005 with exponential back-off

## Observability

- Prometheus metrics at `:8001/metrics` (solve count, strategy
  win-rate, latency histograms)
- Structured JSON logs via structlog (`src/log.py`); per-strategy
  events have `auction_id` context
- Daily check-ins via `scripts/analyze_*.py` and `scripts/estimate_economics.py`

## Configuration

All env-driven, see `src/config.py` and the README's configuration
table. Defaults are tuned for an Alchemy free-tier-equivalent RPC
budget. Paid RPC unlocks higher `ROUTER_MAX_ORDERS`, `MAX_CONCURRENT`,
and `LONG_TAIL_ENABLED=true`.
