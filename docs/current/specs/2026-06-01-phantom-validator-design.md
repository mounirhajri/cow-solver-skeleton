# Phantom-Score Validator — real `settle()` feasibility check in shadow mode

**Author:** mhajri
**Date:** 2026-06-01
**Status:** Design (approved in principle — "Option W", inline going-forward)
**Companion to:** `2026-05-22-cow-solver-design.md`, `phase2-pool-indexer-design.md`

---

## 1. The problem this solves

Every score the solver reports today is **phantom**: `compute_solution_score`
(`src/shadow/scoring.py`) evaluates our own trades against *our own* clearing
prices. It checks limit-price validity but never checks whether the solution
would actually **clear on-chain**. A solution can score beautifully and still
revert in `GPv2Settlement.settle()` because:

- the AMM interaction we encoded doesn't move the tokens we promised
  (phantom liquidity — the V3 path was never simulated; `price_refiner.py`
  documents this as KNOWN-BAD), or
- token conservation fails (we credit a buy-token amount the settlement can't
  cover).

The 48h verification (2026-05-31) showed we "win" 0% of the surplus-heavy
buckets and that **3 of 5** sampled solutions reported a score *above* the
actual winner yet lost. We cannot tell whether those covered-but-losing
solutions are "real, lose on quality" or "phantom, would revert". That
distinction is the #1 blocker before any strategy work (joint clearing, MP
revival).

Two distinct problems live under "phantom":

- **Problem A — fee disparity.** Our score is pre-fee (correct for shadow
  mode); the winner's published score is post-fee. `win_rate_hypothetical`
  overstates our advantage. *This is a pure-scorer fix, handled separately in
  §7.*
- **Problem B — feasibility.** Does our calldata actually settle? *This spec is
  about Problem B.*

### Why not just onboard to Barn?

Barn (Phase 0a) is the official path — the CoW driver natively simulates and
scores our solutions in a staging sandbox. We still want it. But:

1. It requires a Telegram call + registered submission key (lead time, and the
   user does not want to flag the CoW team and *then* discover a bug).
2. The driver we already run locally is RPC-hungry — it 429s against Alchemy
   while *idle*. A full driver-in-the-loop simulator would multiply that load,
   and **we are not upgrading the RPC tier**.

So we build a **lightweight, private, self-hosted feasibility check** that
proves a solution settles before we ever claim it would have won. It validates
our numbers privately so the eventual Barn conversation starts from verified
ground.

---

## 2. Approach: backfill real signatures, build a real `settle()`, `eth_call` it

The captured auction data (`raw_auction`) has every order field **except the
signature** — so we cannot forge a settlement ourselves. But the CoW orderbook
exposes each order, signature included, at a **public, ungated** endpoint:

```
GET https://api.cow.fi/arbitrum_one/api/v1/orders/{uid}
→ { ..., "signature": "0x…", "signingScheme": "eip712" | "eip1271" | "presign" | "ethsign", ... }
```

(Confirmed via WebFetch 2026-06-01. Unlike `/auction`, which is 403-gated for
registered solvers, `/orders/{uid}` is open.)

With the real signature we can assemble a **real** `GPv2Settlement.settle(...)`
calldata for our solution and run it as a read-only `eth_call`:

- **reverts** → the solution is **phantom / infeasible**. Record the revert.
- **succeeds** → the solution is **commit-feasible**. The score is real.

Mixed signing schemes (eip712 EOA, eip1271 smart-contract, presign) need no
special handling on our side — the real settlement contract verifies each
signature exactly as it would in production. We just pass the bytes through.

### Block strategy: inline, going-forward (chosen)

We validate **at the time we persist the solution**, against `block = "latest"`.

- No archive RPC needed (we never re-simulate a historical block). This fits
  the no-tier-upgrade constraint.
- The settlement we simulate is a few seconds stale relative to the auction
  deadline — acceptable: we're proving *feasibility of the shape*, not
  reproducing the exact winning block. A solution that clears at latest would
  have cleared at deadline barring adversarial within-block moves.
- Trade-off accepted: we do **not** back-validate the historical backlog. Only
  solutions persisted after this ships get a `feasible` verdict. Older rows
  stay `NULL` (unknown). That's fine — the analytics question is "are our
  *current* solutions real", and going-forward answers it within a day.

---

## 3. Components

```
src/shadow/cow_api.py        (+ fetch_order)        — get the real signature
src/encoder/settle.py        (NEW)                  — build settle() calldata
src/routing/rpc.py           (extend eth_call)      — from + optional revert capture
src/shadow/feasibility.py    (NEW)                  — orchestrate one validation
src/shadow/persist.py        (hook)                 — call it inline at persist
src/persistence/models.py    (+ columns)            — store the verdict
alembic migration            (NEW)                  — feasible + revert_reason
```

### 3.1 `cow_api.fetch_order(uid)` — signature backfill

Add a method mirroring the existing `fetch_competition` urllib pattern (the
CoW API blocks httpx's TLS fingerprint; UA stays `curl/8.5.0`). Returns the
order dict or `None` on 404.

**Caching is mandatory.** CloudFront rate-limits aggressively (the existing
`sync_competitions.py` already caches for this reason). An order's signature is
immutable once signed, so cache the full order JSON keyed by uid with a long
TTL (reuse the Redis layer; 7-day TTL like the pool-address cache). A given uid
is fetched at most once.

### 3.2 `encoder/settle.py` — the `settle()` ABI encoder (NEW, the real work)

`GPv2Settlement.settle` signature:

```solidity
function settle(
    IERC20[]        tokens,
    uint256[]       clearingPrices,
    GPv2Trade.Data[] trades,
    GPv2Interaction.Data[][3] interactions   // [pre, intra, post]
)
```

We already have `Interaction` + `to_gpv2_dict` (`src/encoder/interactions.py`)
and V2/V3 calldata builders. What's missing is the **trades[] encoder** and the
top-level tuple-array ABI packing. Each `GPv2Trade.Data`:

```solidity
struct Data {
    uint256 sellTokenIndex;   // index into tokens[]
    uint256 buyTokenIndex;
    address receiver;
    uint256 sellAmount;
    uint256 buyAmount;
    uint32  validTo;
    bytes32 appData;
    uint256 feeAmount;
    uint256 flags;            // bitfield: order kind | partiallyFillable | sell/buy-token-balance | signing scheme
    uint256 executedAmount;
    bytes   signature;
}
```

The `flags` bitfield encoding (kind bit, partial-fill bit, balance source,
signing scheme) follows the GPv2 spec exactly — this is the part to unit-test
against known-good fixtures (encode a real settled order, byte-compare to the
on-chain tx that settled it). Token list + clearingPrices come straight from
our `solution.prices`; the interactions array wraps our solution's
`interactions` in the intra slot (pre/post empty for our AMM paths).

Use `eth_abi` (already a transitive dep via web3) for the tuple packing. The
selector is `keccak("settle(address[],uint256[],(uint256,uint256,address,uint256,uint256,uint32,bytes32,uint256,uint256,uint256,bytes)[],(address,uint256,bytes)[][3])")[:4]`.

### 3.3 `rpc.eth_call` — add `from`, capture revert

`settle()` has the `onlySolver` modifier, so the simulated call must come
**from an allowlisted solver address**. Extend `eth_call` to accept an optional
`from_addr` and inject it into the params object:

```python
params = [{"from": from_addr, "to": to, "data": data}, block]
```

The allowlisted solver address goes in config (a known existing CoW solver, or
our own registered address once Barn lands). For now we use a public solver
address that is allowlisted on the live settlement contract — the call is
read-only (`eth_call`), so we never need its key.

On revert, the node returns a JSON-RPC error. Today `eth_call` raises a generic
`RuntimeError` on non-rate-limit errors. We need the **revert reason**, so add a
mode (or a sibling method `eth_call_capture`) that returns
`(success: bool, result_or_reason: str)` instead of raising — feasibility wants
the revert string, not an exception.

### 3.4 `shadow/feasibility.py` — orchestrate one validation (NEW)

```
validate_solution(solution, orders_by_uid, settlement_addr, solver_addr) -> Verdict
  1. For each fulfillment trade's uid: fetch_order(uid) → real signature
     (cache hit usually). If any order can't be fetched → verdict UNKNOWN.
  2. Build tokens[], clearingPrices[] from solution.prices.
  3. Build trades[] (encoder/settle.py) with real signatures + executedAmount.
  4. Wrap solution.interactions into the intra slot.
  5. ABI-encode settle() calldata.
  6. rpc.eth_call_capture(from=solver_addr, to=settlement_addr, data=calldata,
     block="latest").
  7. success → FEASIBLE; revert → PHANTOM (+ reason); fetch/encoding failure →
     UNKNOWN (+ reason).
```

`Verdict` = `(feasible: bool | None, reason: str | None)`. `None` feasible =
UNKNOWN (don't punish a solution for our own infra gap).

### 3.5 `persist.py` hook — inline at persist time

At the existing scoring callsite (`persist.py:~130`, where
`compute_solution_score` runs), after a non-null score is computed for a
*submittable* strategy (skip `naive` — already excluded), call
`validate_solution` and stash the verdict on the new columns. Guard the whole
thing in `contextlib.suppress(Exception)` like the surrounding code: a
feasibility infra failure must never break persistence.

**Cost control:** only validate solutions that (a) belong to a submittable
strategy and (b) have a positive computed score (a phantom-suspect score above
`EPSILON_HIGH_WEI` is *exactly* what we want to catch, so validate those too —
they're already NULL-scored but the verdict tells us *why*). This bounds us to
~a handful of `eth_call`s per auction, well within the RPC budget that the
idle driver was blowing.

### 3.6 Model + migration

Add to `ShadowSolution`:

```python
feasible:      Mapped[bool | None]  = mapped_column(Boolean)   # None = not yet validated / UNKNOWN
revert_reason: Mapped[str | None]   = mapped_column(Text)      # populated when feasible is False
```

Alembic migration following `d86057a796cb_add_our_score_wei.py`. Both nullable
for backward compat — every existing row is `NULL` (unknown), matching the
going-forward semantics.

---

## 4. Data flow (one solution, end to end)

```
solver produces Solution ─► persist.py
                              │
                              ├─ compute_solution_score  (existing, our prices)
                              │
                              └─ validate_solution        (NEW)
                                   │
                                   ├─ fetch_order(uid)     ─► Redis cache ─► CoW /orders/{uid}
                                   ├─ encode settle()      ─► tokens, prices, trades(+sig), interactions
                                   └─ eth_call_capture     ─► Arbitrum node @ latest
                                        │
                                        ├─ success ─► feasible=True
                                        └─ revert  ─► feasible=False, revert_reason="…"
                                   │
                              ShadowSolution row: our_score_wei + feasible + revert_reason
```

---

## 5. What this unblocks

- **Lever diagnosis.** The blocked question — "are covered-but-losing big-bucket
  solutions real or phantom?" — gets a per-row answer. `feasible=True` +
  losing → real, lose on quality (strategy problem). `feasible=False` → phantom
  (encoding/liquidity problem). Different fixes; now distinguishable.
- **Honest analytics.** `validate_data` / `verify_24h` can filter to
  `feasible=True` rows. The retracted ★ "würden gewinnen" markers can be gated
  on a real feasibility verdict instead of a phantom score.
- **Barn readiness.** When we do onboard, we walk in with privately-verified
  feasible solutions — no "flag the team then find a bug" risk.

---

## 6. Failure modes & guardrails

| Failure | Handling |
| --- | --- |
| CoW `/orders/{uid}` 404 (order purged) | Verdict UNKNOWN, `feasible=NULL`. No crash. |
| CloudFront rate-limit on backfill | Redis cache (7d) makes each uid one fetch ever; on miss-storm, UNKNOWN rather than block persist. |
| Solver address not allowlisted → settle reverts `onlySolver` | Pick a confirmed-allowlisted public solver addr; covered by a smoke test that a known-good historical settlement re-simulates to success. |
| Node returns revert w/o reason string | Store `revert_reason="<no reason>"`; `feasible=False` still recorded. |
| `eth_call` rate-limit (429/-32005) | Existing backoff in `rpc.py` handles it. Bounded call volume (§3.5) keeps us under budget. |
| Encoding bug → false PHANTOM | Smoke test in §8 (re-simulate a real winner's settlement → must be FEASIBLE) catches systematic encoder errors before they poison data. |
| Latest-block state drift vs deadline | Documented limitation; we validate feasibility-of-shape, not exact-block reproduction. |

---

## 7. Problem A (fee disparity) — separate, noted here for completeness

Not part of this spec's code, but the companion fix: `scoring.py` should apply
the order's `feePolicies` (already captured in `raw_auction`) when we want a
fee-aligned comparison against the winner's post-fee score. That's a pure-code
change to the scorer with no RPC/encoding surface. Tracked separately so the
feasibility work (Problem B) can ship independently.

---

## 8. Acceptance

- **Smoke test (the critical one):** take a real, settled winner solution from
  `shadow_winners.raw_solution`, backfill its order signatures, encode
  `settle()`, `eth_call` at a recent block → **must return FEASIBLE**. If a
  known-good settlement comes back PHANTOM, the encoder is wrong. This gates
  the whole feature.
- **Phantom catch:** a hand-constructed solution with an impossible clearing
  price (credits more buy-token than any interaction provides) → **must return
  PHANTOM** with a token-conservation revert reason.
- Unit tests: `flags` bitfield encoding vs known fixtures; trades[] tuple
  packing byte-compared to a real on-chain settle tx.
- `fetch_order` cache: second call for the same uid hits Redis, makes zero HTTP.
- `feasible`/`revert_reason` columns migrate cleanly; existing rows = NULL.
- Persistence never breaks on a feasibility infra failure (suppressed, logged).
- `ruff` / `mypy` clean on touched files; public-clone path still bootstraps.
- RPC call volume per auction stays within the free-tier budget (bounded to
  submittable + positive-score solutions).
```

