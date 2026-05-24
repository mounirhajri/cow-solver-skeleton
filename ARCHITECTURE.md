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
                                Naive excluded from submission
                                  (oracle-priced, shadow-only)
                                          │
                                          ▼
                                  CIP-67 Composer
                                   (strict token-disjoint:
                                    any overlap → reject candidate)
                                          │
                                          ▼
                                  EBBO pre-submission check
                                   (V3 quote vs our clearing
                                    price, ±50 bps tolerance)
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

**Important: naive is never the submitted solution.** Its clearing
prices still over-state realised settlement (oracle prices and spot
quotes are not the same as final batch-cleared prices). The orchestrator
filters naive out of composer candidates and the first-winner fallback,
so naive only ever appears in `shadow_solutions` for analysis — never
in the driver response.

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

**Ring cooldown**: after emitting a ring, every involved order UID
goes into a per-solver cooldown cache with a `MULTI_PARTY_RING_COOLDOWN_SECONDS`
expiry (default 600 s). On subsequent auctions those UIDs are filtered
out of the candidate set BEFORE Johnson runs. This mirrors on-chain
TWAP behaviour: after a TWAP chunk is filled, the next chunk is not
available until the interval elapses. Without the cooldown, a single
persistent TWAP order produced the same ring in 400+ consecutive
auctions during shadow runs.

### 4. LongTailRouter (`edge/pool_indexer/long_tail_router.py`)
Lazy-indexed UniV2-style router. Per order, looks up cached pool
addresses (Sushi/Camelot V2) for the sell→buy pair via a Redis-backed
`PoolCache` (`solver:pool:addr:*` keys, 7d TTL for addresses, 60 s for
reserves). Quotes via `routing/amm_v2.py` constant-product math.
Gated by `LONG_TAIL_ENABLED` env var (disabled in prod when on a tight
RPC tier — its concurrent multicalls compete with RouterSolver for the
provider's connection budget).

### 5. RouterSolver (`src/solver/router.py`)
For top-N orders by **expected surplus** (`sell_value − buy_value`
at reference prices — the absolute capture opportunity each limit-order
leaves on the table), requests V3 QuoterV2 quotes across 4 fee tiers
(direct + 2-hop via configured intermediates) and picks the best per
order. OTM orders (negative margin at reference) clamp to a sort key
of 0 so they sort to the back rather than dominating with a large
absolute value. Falls back to ETH-value when either token of the order
lacks a reference price.

Handles both order kinds: sell orders use `quoteExactInputSingle`
(profitable if `amount_out ≥ buy_amount`); buy orders use
`quoteExactOutputSingle` with reversed path encoding for multi-hop
(profitable if `amount_in ≤ sell_amount`). Trade emission follows the
CoW convention — `executedAmount` is always the order's exact side
(buyAmount for buys, sellAmount for sells).

Two modes via `ROUTER_V3_ONLY_BATCHED`:

- **Batched** (default, `true`): all candidate paths for the whole
  auction are batched into one Multicall3 round-trip (`routing/v3_batched.py`).
  ~80× RPC reduction vs the legacy per-order fan-out
- **Legacy**: per-order `asyncio.gather` over `quote_best_path`
  (kept for the public clone case where V3 quoters might be unavailable)

## EBBO pre-submission validator (`src/solver/ebbo.py`)

Before returning a Solution to the driver, the orchestrator verifies
each emitted sell trade against a fresh Uniswap V3 quote of the same
swap at the same input size. If our effective user output
(executed_sell × clearing_price_sell / clearing_price_buy) falls below
the external quote by more than `EBBO_TOLERANCE_BPS` (default 50 bps),
we reject the whole composed Solution and fall through to NoSolution.

Why this matters: multi-party rings derive ring-internal anchor-
relative prices that are mathematically consistent within the ring but
not directly compared to external market prices. A ring whose internal
rates underprice a hop will silently produce an EBBO-violating
composed solution if we don't check. Shipping that to mainnet risks
driver rejection or bond slashing.

Skipped (not failed) cases: buy-kind orders (router-v2 supports
quoteExactOutput but the validator wiring is follow-up work), trades
missing clearing prices, trades whose tokens have no external V3
route, and quoter exceptions (RPC blips should not deny revenue).
Solution is rejected only on a TRUE shortfall.

The validator shares the orchestrator's Multicall3 instance — no
extra RPC connection budget.

## CIP-67 composer (`edge/matching/composer.py`)

When multiple strategies return non-empty solutions, the composer
merges them subject to:

- **Naive is filtered out at the orchestrator level** before reaching the
  composer; its oracle-derived prices are not submission-grade
- **Per-order disjointness**: an order can appear in at most one trade
- **Strict token-disjoint composition**: a candidate whose
  `solution.prices` overlap **any** already-claimed token is rejected
  wholesale — no averaging, no partial trade leakage. Every token in
  the composed `Solution.prices` comes from exactly one solver

The "average prices within ±2 % tolerance" variant of this composer
previously produced ~480 ETH per-win fantasy CIP-14 scores in shadow
when it mixed multi-party's anchor-relative ring prices with router-v2's
market prices. Strict disjointness leaves marginal surplus on the table
but is provably CIP-67-compliant; the alternative shipped unexecutable
solutions.

The Multi-Party solver additionally enforces *ring-level* token
disjointness internally (see `multi_party.py` composer block) because
each ring derives its own anchor-relative clearing prices and those are
not interchangeable across rings.

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
  gracefully (only naive + router-v2 run; naive is still excluded from
  submission, so a public clone with no edge submodule and a working
  router-v2 still produces submission-grade output, but a clone with
  *only* naive falls through to NoSolution rather than submit fantasy
  trades)
- All clearing-price logic respects user limits at the prices we claim
  (LP enforces `s_i × x_{i+1} ≥ b_i × x_i` in atoms; clearing prices are
  derived such that the rate equals or exceeds each user's limit rate)
- The reconcile loop tolerates corrupted JSONL lines: each line is
  parsed in its own try/except and corrupt lines are dropped on the
  next rewrite, so a single bad record from a crash or disk-full event
  cannot tear down the loop forever
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
