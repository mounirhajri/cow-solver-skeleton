# Partial Fills — Implementation Plan

**Source spec:** `docs/superpowers/specs/2026-05-25-partial-fills-design.md`
**Authored:** 2026-05-24 (evening, post phantom-surplus incident wrap-up)
**Total estimate:** 5–10 working days, decomposed into 4 sub-PRs
**Critical-path phase:** Phase 1 (LP rounding) — production-safety blocker before any submission

## Why decompose

The spec estimates "1–2 weeks" as a single block. Decomposing buys:
- Each sub-PR is independently reviewable (≤ 300 LOC each)
- Phase 1 ships the safety-critical fix without waiting for the revenue-positive changes
- Audit (Phase 0) lets us cut a phase if the upside isn't there
- Composer + EBBO regressions are caught after each sub-PR, not a single big-bang

## Phase 0 — Audit (this PR, ~1 h)

**Goal:** confirm the 30 % `partiallyFillable=true` estimate before committing to Phases 1–3.

**Concrete query:**

```sql
SELECT
  COUNT(*) FILTER (WHERE partial) AS n_partial,
  COUNT(*)                         AS n_total,
  ROUND(100.0 * COUNT(*) FILTER (WHERE partial) / NULLIF(COUNT(*), 0), 1) AS pct_partial
FROM (
  SELECT (o->>'partiallyFillable')::bool AS partial
  FROM shadow_auctions, jsonb_array_elements(raw_auction::jsonb->'orders') AS o
  WHERE polled_at > now() - interval '7 days'
) t;
```

**Decision rule:**
- `pct_partial ≥ 20 %` → Phase 1 + 2 mandatory (revenue + safety)
- `pct_partial < 20 %` AND `pct_partial ≥ 5 %` → Phase 1 only (safety), defer 2 + 3
- `pct_partial < 5 %` → Phase 1 only, deprioritise everything else

**Deliverable:** numeric result pasted into this doc + commit to spec.

## Phase 1 — LP rounding fix (edge submodule, ~2 d)

**Critical-path. Cannot submit live without this.**

### Files
- `edge/matching/surplus.py:198-229` — `solve_ring_lp` rounding
- `edge/matching/multi_party.py:228, 279` — emission

### Changes

1. **`RingLPResult`:** add `executed_amounts: list[int]` (per-leg, already
   computed but currently dropped in favor of `sell_amount[i]` at emission
   time).
2. **`solve_ring_lp`:** round `x_real[i]` DOWN (`int(floor(x_real[i]))`).
   Persist as `executed_amounts[i]`.
3. **`multi_party.py` emit:** `executed_amount = result.executed_amounts[i]`
   not `o.sell_amount`. For non-partial orders: if
   `executed_amounts[i] < o.sell_amount`, drop the entire ring as
   infeasible (one short leg breaks settlement).
4. **`partial_fillable` propagation:** thread `o.partially_fillable` from
   `multi_party.py` → LP → result. LP can refuse to emit a fractional fill
   for a non-partial order BEFORE solving (saves CPU).

### Tests
- `test_lp_emits_floor_rounded_executed_for_partial_ring`
- `test_lp_drops_ring_when_non_partial_short_after_round`
- `test_lp_full_fill_unchanged_when_no_fractional` (regression)

### Risk
- Multi-party median surplus may drop slightly (1–3 %) due to floor-rounding
  vs the current implicit ceil-via-overfill behavior. That's the *correct*
  number; the previous number was unsafe.

## Phase 2 — Bipartite partial pairing (edge submodule, ~1 d)

**Pure revenue. Optional after Phase 0 audit.**

### Files
- `edge/matching/bipartite.py:_match_pair, _emit_matched_pair`

### Changes

Greedy matcher today: skip pair if `o_a.sell_amount != o_b.buy_amount` (or
the buy/sell limit equivalent). With partials:

1. **Detect mismatched-volume pairs:** if at least one side is partial, match
   at `min(o_a.sell_amount, o_b.buy_amount)` rather than skipping.
2. **Emit two trades with `executedAmount = min_volume`** for each side.
3. **Score correctly:** CIP-14 `_score_*_trade` is linear in `executed`, so
   no scoring change needed.

### Tests
- `test_bipartite_partial_pair_matches_at_min_volume`
- `test_bipartite_skips_pair_when_both_non_partial_and_volumes_differ`
- `test_bipartite_score_proportional_to_executed`

## Phase 3 — Router partial quote search (skeleton, ~1 d)

**Optional. Profile first against shadow.**

### Files
- `src/solver/router.py:_solve_v3_batched` (sell path)

### Changes

For sell orders where the full-`sell_amount` quote misses limit:
1. Issue **one** extra `quoteExactInput` at `0.5 × sell_amount`.
2. If that clears the limit: binary-search **once** at the midpoint of
   `[0.5, 1.0] × sell_amount`.
3. Emit at the largest feasible point (max 2 extra V3 calls per order).

Buy-side partial-quote: deferred — `quoteExactOutput` semantics for partials
are subtle (the protocol clamps to `buyAmount`).

### Tests
- `test_partial_quote_search_emits_at_50pct_when_full_misses`
- `test_partial_quote_search_emits_at_75pct_when_50_clears_75_clears`
- `test_partial_quote_search_no_op_when_full_clears`
- `test_partial_quote_search_bounded_to_3_calls_total`

### Risk
- +2 RPC calls per missed order = +4–8 % RPC budget. Profile against current
  v3_batched cost; abort if it pushes us past Alchemy free-tier headroom.

## Phase 4 — Integration test + live verification (~1 d)

### Test

`tests/test_solver/test_partial_fills_integration.py` — two new tests:

1. **`test_partial_fillable_sell_emits_partial_solution_end_to_end`** —
   `partiallyFillable=True` sell order, full(1000)→800 miss, 0.75x(750)→700
   clears. Asserts `Solution` with `executed_amount=750` (< `sell_amount=1000`).

2. **`test_non_partial_sell_full_miss_emits_no_solution_end_to_end`** —
   identical AMM mock, `partiallyFillable=False`. Asserts `NoSolution`
   (regression guard: partial-quote-search must not fire for non-partial orders).

Both tests run through `RouterSolver._solve_v3_batched` (Phase 3) with a
mocked `batched_v3_quote`. Bipartite / multi-party integration tests are NOT
included — those strategies require the `edge` submodule (LP solver + ring LP);
a separate suite should cover them once `edge` is installed in CI.

### Live verification

Run this query **after** `feat/partial-fills` deploys to production. It scans
the last 24 hours of shadow data and returns one row per strategy where the
router emitted a partial fill (`executedAmount < sellAmount / buyAmount`).
Non-zero `n_partial_emissions` with `avg_pct_filled` in the 50–90 % range
confirms the code path is alive. Absence of any row means the partial-fill
emission path is dead — either the deploy failed or the rounding/flag check
is broken.

```sql
-- Run AFTER feat/partial-fills deploys to prod. Confirms the partial-fill
-- emission path is alive in real shadow data. Absence of any row =
-- dead code path; presence with sensible avg_pct_filled (~50-90%) = working.
SELECT
  strategy,
  COUNT(*) AS n_partial_emissions,
  MIN(executed_amount) AS min_executed,
  MAX(executed_amount) AS max_executed
FROM (
  SELECT
    s.strategy,
    (t->>'executedAmount')::numeric AS executed_amount,
    (t->>'orderUid') AS uid
  FROM shadow_solutions s,
       jsonb_array_elements(s.solution::jsonb->'trades') t
  WHERE s.created_at > now() - interval '24 hours'
    AND s.status = 'solved'
    AND s.strategy IN ('router-v2', 'cow-matching-bipartite', 'cow-matching-multi-party')
) e
JOIN (
  SELECT
    (o->>'uid') AS uid,
    (o->>'sellAmount')::numeric AS sell_amount,
    (o->>'buyAmount')::numeric AS buy_amount,
    (o->>'kind') AS kind
  FROM shadow_auctions a,
       jsonb_array_elements(a.raw_auction::jsonb->'orders') o
  WHERE a.polled_at > now() - interval '24 hours'
    AND (o->>'partiallyFillable')::bool = true
) ord ON ord.uid = e.uid
WHERE
  -- For sell-kind orders, executed is the sell side; for buy-kind, the buy side.
  (ord.kind = 'sell' AND e.executed_amount < ord.sell_amount)
  OR
  (ord.kind = 'buy' AND e.executed_amount < ord.buy_amount)
GROUP BY strategy
ORDER BY n_partial_emissions DESC;
```

## Open questions (carry from spec)

1. **Buy-order `executedAmount` for partial fills:** currently
   `executedAmount = buy_amount` (full). For partial buys we need
   `executedAmount = partial_buy_amount`. Confirm by re-reading
   `_score_buy_trade` mid-Phase-1.
2. **Fee policy interaction:** `feePolicies[]` is opaque; CoW docs say fees
   scale linearly with executed fraction. Verify on a fixture.
3. **TWAP chunk + partial within chunk:** plausible win source. Measure
   post-deploy in Phase 4.

## Dependency on the price_refiner KNOWN-BAD comment

The spec mentions price_refiner as "real fix needs CIP-67-uniform clearing
prices (LP over per-pair AMM rates) — tracked as partial-fills design
dependency." This plan does NOT include that work. The KNOWN-BAD comment
in `src/solver/price_refiner.py` stays. Reasoning:
- price_refiner is only called by NaiveSolver
- Naive is excluded from submission by existing tests
- Fixing price_refiner requires a different LP than partial-fills — it
  solves CIP-67 uniform clearing across multiple pairs, not per-order
  fractional execution
- Conflating the two would blow this plan's scope past 2 weeks

If a future spec needs both, do partial-fills first (this plan), then
write a separate CIP-67-uniform-clearing-prices spec.

## Out of scope (explicit)

- Multi-step partials across auctions (a stretch goal, separate spec)
- Atomic-bundle interaction (per CoW 2026-05-01 article, wraps settlement
  at protocol level, doesn't change per-order partial semantics)
- Partial fills in EBBO validator (the validator is read-only over
  Solution.trades; if `executedAmount < signed_amount` it just checks the
  AMM-rate at that executed amount, no change needed)

## Sequencing recommendation

```
Phase 0 (audit, this PR) ──┬──→ Phase 1 (LP rounding) ──→ Phase 4 (verify)
                            │                              ↑
                            ├──→ Phase 2 (bipartite)  ─────┘
                            │
                            └──→ Phase 3 (router) ─────────┘  (gated on cost-profile)
```

Phase 1 is the only critical-path item. 2 and 3 are parallelizable after Phase 0 audit. Phase 4 runs after all enabled phases land.
