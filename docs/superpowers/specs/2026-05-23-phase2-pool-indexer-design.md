# Phase 2 — Long-Tail Pool Indexer (MVP)

**Author:** mhajri
**Date:** 2026-05-23
**Status:** Implemented (MVP)
**Companion to:** `2026-05-22-cow-solver-design.md`

---

## 1. Goal

Replace the Phase-1 `LongTailRouter` stub (returns `NoSolution`) with a working
direct-AMM router that quotes Uniswap-V2-style pools (sushi + camelot on
Arbitrum) for orders the main `RouterSolver` does not fill, **as a margin
lever** alongside the existing solver chain — not a replacement.

The MVP target is "good enough to clear extra batches at break-even gas on
Arbitrum" (the G6 break-even row from the revenue strategy spec). Pre-warming,
factory watching, V3, and multi-hop edge routing are deliberately deferred.

---

## 2. Lazy Indexer (no factory watcher)

We considered three architectures:

1. **Lazy indexer (chosen):** pools enter the cache on first encounter. No
   background process.
2. **Factory watcher:** subscribe to `PairCreated` events via WebSocket, pre-warm
   the cache so the first auction touching a pair has zero RPC overhead.
3. **Periodic full scan:** crawl the factory event history every N minutes.

The lazy approach wins for the MVP because:

- Most CoW-relevant pairs already touch a Camelot or Sushi pool that existed
  for weeks; the cold-cache RPC penalty is paid once per pair, then amortised
  over many auctions.
- Pool addresses are content-addressed by `(factory, token_a, token_b)` and
  effectively immutable, so a 7-day TTL is plenty.
- No background daemon means no extra process to monitor, supervise, or restart
  on RPC flapping. Production complexity drops to "one Redis + one strategy
  callsite".
- The factory watcher's only real benefit is faster cold quotes, and the
  short-tail of pairs we *actually* trade is small enough that lazy fetch will
  saturate the cache within hours of going live.

Trade-off accepted: the first auction touching a brand-new pair pays ~300 ms of
RPC. That's well under the 9 s strategy timeout and only happens once per pair.

---

## 3. Cache layout

Two Redis namespaces under the existing `settings.redis_key_prefix`
(default `solver:`):

| Key | Value | TTL | Why this TTL |
| --- | --- | --- | --- |
| `solver:pool:addr:{lo}:{hi}` | JSON `{factory: pool_addr}` | 7 days | Pool addresses don't change. 7d keeps the cache fresh for the long-tail and evicts dead pairs eventually. |
| `solver:pool:reserves:{pool_addr}` | JSON `{reserve0, reserve1, token0, token1}` (ints as strings) | 60 seconds (configurable via `pool_cache_ttl_seconds`) | Reserves move with every swap. 60 s bounds quote staleness while keeping cache hit rate high during a single batch wave. |

**Pair-key ordering:** the two token segments are sorted lexicographically and
lower-cased so `(A,B)` and `(B,A)` hit the same slot. Without this we burn a
slot and an RPC on every orientation flip.

**Big-int safety:** UniV2 reserves are uint112 (up to 2¹¹²), well past the JS
`Number.MAX_SAFE_INTEGER`. We serialise reserves as JSON strings so any
downstream consumer (including the Node-based shadow-data pipeline) reads them
losslessly. Test coverage: `test_reserves_roundtrip_preserves_big_ints`
exercises a 2¹¹⁰ value.

---

## 4. Solve pipeline

```
1. Sell-only orders, sorted by ETH-equivalent value (reuses _eth_value_sort_key
   from src.solver.router so RouterSolver and LongTailRouter prioritise the
   same orders).
2. Top max_orders=30 orders, run in parallel with Semaphore(max_concurrent=3)
   — matches RouterSolver's Alchemy free-tier budget.
3. Per order:
     a. Lookup pool addresses for (sell, buy) → cache hit OR find_pool_addresses
     b. For each pool: lookup reserves → cache hit OR fetch_reserves
     c. quote_v2_swap(sell_amount, reserve_in, reserve_out, fee_bps=30) for
        each factory, take the best amount_out.
4. Emit Trade when best_amount_out >= buy_amount. Clearing prices: prefer
   auction reference_price (CIP-14 ETH-denominated); fall back to execution
   ratio when references are missing.
```

The router emits the same `Solution` shape as `RouterSolver`, so the composer
treats it as just another candidate.

---

## 5. Performance budget

| Step | Latency | Notes |
| --- | --- | --- |
| Cache hit (Redis GET) | ~1 ms | Local Redis on same host |
| Cache miss for addresses | ~150 ms | Multicall3, both factories in one RPC |
| Cache miss for reserves | ~150 ms | Multicall3, all pools in one RPC |
| Per-order worst case | ~300 ms | Both cache layers cold |
| Top-30 orders, 30% miss rate, max_concurrent=3 | ~3 s | 21 hits × 1 ms + 9 misses × 300 ms in 3 waves |

Strategy timeout: **9 s**. The 3 s estimate leaves ~6 s of headroom for RPC
tail latency on the public Arbitrum endpoint.

---

## 6. Phase 2.5 — what we deferred

These are listed in priority order; each is a self-contained follow-up.

1. **Factory watcher / pre-warmer.** Background process that subscribes to
   `PairCreated` on sushi + camelot factories via WebSocket, populates the
   address cache during off-hours. Reduces cold-quote latency to zero. Worth
   doing once we see how often a brand-new pair shows up in an auction.
2. **Multi-hop V2.** Today's long-tail router only quotes direct pairs;
   RouterSolver handles WETH-intermediate hops. Eventually unify them.
3. **UniV3 / Camelot V3 quotes.** Larger TVL, tighter spreads, but quoter math
   is ~5× more complex. Out of scope for the MVP.
4. **Negative-result caching with shorter TTL.** Currently a missing pair is
   cached for 7 days (same as a hit). If we see lots of new-pair traffic, drop
   that to a few minutes.

---

## 7. Acceptance

- All previous `tests/` pass — no regressions.
- New tests: `tests/test_edge/test_pool_cache.py`,
  `tests/test_edge/test_long_tail_router.py`.
- `ruff` / `mypy` clean on touched files.
- Public-clone path (`edge/` missing) still bootstraps cleanly via the
  orchestrator's `except ImportError`.
- Redis keys under `solver:pool:*` use the configured `redis_key_prefix`.
