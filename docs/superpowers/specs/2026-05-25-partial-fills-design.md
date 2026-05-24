# Partial Fills Design

**Status:** draft / not started.
**Authored:** 2026-05-24 (post EBBO + EIP-1271 + Buy-Orders sprint).
**Estimated effort:** 1–2 weeks.
**Owner:** TBD.

## Problem

CoW orders may carry `partiallyFillable = true`. For these the acceptance
set is a continuum: any executed amount `x ∈ (0, sellAmount]` (sell-kind)
or `y ∈ (0, buyAmount]` (buy-kind) is valid as long as the rate honours
the user's limit. Fees scale pro-rata with `executedAmount / sellAmount`
(or `executedAmount / buyAmount` for buys).

Today every strategy treats `partiallyFillable` as informational only:

- `RouterSolver` always quotes the full `sellAmount` (or full
  `buyAmount` for buys); if the V3 quote misses the limit by 1 atom we
  emit nothing instead of emitting a partial fill that would clear.
- `BipartiteMatcher` greedy-by-largest matches whole orders — when the
  two counter-orders aren't equal-volume the smaller side bounds the
  whole match, leaving the larger order untouched even though a partial
  fill at the matched volume would still satisfy its limit.
- `CoWMatchingSolver`'s LP variable `x_i` is already continuous in
  `[0, sell_amount_i]`, but the trade emission does
  `floor(x_real[i])` and writes `executedAmount = full_chunk_amount` —
  meaning over-fill emission whenever the LP returns a strict-fractional
  optimum. In shadow this is harmless; in production a settlement that
  over-fills any partial order reverts.

## Impact estimate

- ~30 % of CoW Arbitrum orders carry `partiallyFillable = true` (mostly
  Safe-TWAP chunks + institutional limit orders, by observation in the
  shadow JSONL — to be confirmed by a `select count(*)` query).
- Of those, roughly half are currently being SKIPPED because the LP /
  bipartite math finds them un-matchable as full-fills but matchable as
  partials. Rough upside: **+15 to 25 % win volume**, +€100–300 / month
  at current ETH price.
- Production-safety upside is higher: today the multi-party LP can emit
  fractional executed amounts that get floored to a full-chunk emission
  — that's an over-fill bug that would revert on chain.

## Surface area

| Module | Change |
| --- | --- |
| `edge/matching/surplus.py` | LP variables already continuous. Change `executed[i] = int(floor(x_real[i]))` to round to whatever atom precision the order allows; emit `executedAmount = executed[i]` (not `sell_amount[i]`); add a `partial_fillable` flag to `RingLPResult` so multi-party can refuse to emit fractional fills for non-partial orders. |
| `edge/matching/multi_party.py` | Pass `o.partially_fillable` through to the LP. After the LP, if any non-partial order ended up with `x_i < sell_amount_i`, drop that ring as infeasible. |
| `edge/matching/bipartite.py` | When two counter-orders have different volumes and at least one is partial, match them at the smaller volume rather than skipping. |
| `src/solver/router.py` | Add an optional partial-quoting step: if `quoteExactInputSingle(sell_amount)` misses the limit, binary-search a feasible sub-amount in `[0.5, 1.0] × sell_amount` (for partial sells), emit the largest profitable one. Bounded RPC budget — at most 3 extra quoter calls per order. |
| `src/shadow/scoring.py` | `_score_sell_trade` / `_score_buy_trade` already take `executed` as a parameter; no change. Confirm CIP-14 surplus formula still works at fractional fills (it does — it's linear in `executed`). |
| Trade model | `executedAmount` already a free `int` field. No model change. |

## Order of work

1. **Audit:** count `partiallyFillable=true` orders in 7-day shadow.
   Confirm the 30 % estimate before committing two weeks.
2. **LP rounding fix** (`surplus.py` + `multi_party.py`) — this is the
   production-safety blocker. Tests: feasible partial ring emits
   correctly-rounded executed amounts; infeasible-as-partial ring is
   rejected when any non-partial order is short.
3. **Bipartite partial pairing** — straightforward extension of the
   greedy matcher; same Trade emission convention.
4. **Router partial quote search** — bounded binary-search, optional.
   Only useful for orders that miss the limit by < ~10 %. Profile
   first against shadow data.
5. **Integration test on a known-partial fixture auction.**
6. **Live verification post-deploy:** look for `executedAmount <
   sellAmount` in `shadow_solutions.solution`. Should appear; absence
   means the rounding path is dead.

## Out of scope

- Multi-step partial fills across multiple auctions (a single auction
  fills part, the rest carries over to the next). This is a stretch
  goal that pairs with the multi-auction LP — separate spec.
- Atomic-bundle interaction. Per the 2026-05-01 CoW article, atomic
  bundles wrap settlement at the protocol level; they don't change
  per-order partial semantics.

## Open questions

- **`executedAmount` for buys with partial fills:** confirm by reading
  `_score_buy_trade` whether the executed field stays "buy side
  exact" (e.g. `executedAmount = partial_buy_amount`) or flips. The
  buy-orders subagent emitted `executedAmount = buyAmount` for
  full-fill buys; partials should logically be a fractional buy.
- **Fee policy interaction:** `feePolicies[]` is opaque to us today.
  Per CoW docs fees scale linearly with executed fraction; verify on a
  fixture that we don't double-charge.
- **TWAP chunk + partial fill within chunk:** if a TWAP chunk arrives
  as `partiallyFillable = true` we could partially fill within a single
  chunk. Plausible win source — measure post-deploy.
