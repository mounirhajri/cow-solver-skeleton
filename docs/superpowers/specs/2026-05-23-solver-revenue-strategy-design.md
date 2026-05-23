# CoW Solver — Competitive Strategy & Revenue Plan

**Date:** 2026-05-23  
**Status:** Approved · Phase 1 substantially implemented 2026-05-23; gate added (see §6)
**Goal:** Become a registered, revenue-generating CoW Protocol solver earning weekly COW token rewards.

---

## Why We're Building This

CoW Protocol pays registered solvers weekly in COW tokens based on:
1. **Win-rate** — how often your solution beats the competition
2. **Solver surplus** — the difference between the user's limit price and actual execution price (you keep this)

Shadow mode (current phase) is preparation: collect real auction data, compare our solutions to winners, train the classifier, then flip the switch to live competition.

---

## Current State

```
naive:     30/30 solved | p50 6ms   → always solves, oracle prices (inflated score)
router-v2:  0/30 solved | 30 timeout → iterates all 1200 orders, never finishes in 2.6s
CoW matching: 0/30 solved            → no rings found yet, graph too noisy
```

The naive solver appears to win 100% with +12 ETH delta — this is an artifact.
Oracle (Chainlink) prices are used as clearing prices, inflating theoretical surplus.
Real winner scores are ~0.001 ETH. On-chain execution with oracle prices would revert.

---

## Phase 1 — RouterSolver Order-Cap (✅ implemented 2026-05-23)

### Status

Implemented in commits `ac4fa51` (concurrency tuning + WETH-only intermediates) and the
2026-05-23 router patch (ETH-value sort key). What landed:

- Order-cap (`router_max_orders = 9` for Alchemy free tier)
- `asyncio.gather` + Semaphore (`router_max_concurrent = 3`)
- Per-strategy 11 s timeout exposed via `RouterSolver.timeout`
- HTTP 429 / JSON-RPC -32005 retry with exponential back-off (0.2 s / 0.6 s / 1.8 s) in
  `src/routing/rpc.py`
- ETH-value sort: `sell_amount * reference_price // 10**18`, fallback to raw
  `sell_amount` when reference price missing

### Open operational risk

The free Alchemy tier still produces sustained `RPC error 429` under load — the
3-step back-off exhausts before the 11 s budget. **Dedicated RPC (Alchemy paid /
QuickNode / own node) is a hard prerequisite for Barn**. Decision deferred until
the §6 Go/No-Go gate.

### Original problem (kept for context)
RouterSolver loops over all ~1200 orders and makes on-chain multicall quotes for each.
Per-strategy timeout = 13s ÷ 5 strategies = 2.6s. Always times out. Zero real solutions.

### Fix
Two changes needed together — order-cap alone is not enough:

**1. Order-cap:** Sort by ETH value, take top N before the loop.
```python
# src/solver/router.py  — RouterSolver.solve()
orders = sorted(
    auction.orders,
    key=lambda o: _eth_value(o, auction.tokens),
    reverse=True,
)[:self._order_cap]   # default 30
```
Where `_eth_value` = `sell_amount * token.reference_price // 10**18` (pure math, no I/O).

**2. Parallel quotes:** Current loop is sequential — 30 orders × ~100ms RPC round-trip = 3s,
still over the 2.6s budget. Switch to `asyncio.gather` so all 30 quote concurrently:
```python
paths = await asyncio.gather(
    *[quote_best_path(self._multicall, o.sell_token, o.buy_token,
                      o.sell_amount, self._intermediates)
      for o in orders],
    return_exceptions=True,
)
```
With concurrency: ~100–300ms total for 30 orders (shared multicall batching).

### Files changed
| File | Change |
|------|--------|
| `src/solver/router.py` | `order_cap: int = 30` param; sort+slice; parallel `asyncio.gather` |
| `src/config.py` | `router_order_cap: int = 30` setting |
| `src/solver/orchestrator.py` | pass `settings.router_order_cap` to `RouterSolver` |

### Expected outcome
- RouterSolver finishes in ~100–300ms (30 concurrent multicall quotes)
- Produces solutions with **real DEX prices** — no oracle inflation
- `our_score_wei` becomes an accurate CIP-14 score
- Directly competitive with other solvers in the live competition

### Success metric
`router-v2: X/30 solved | p50 ~300ms` in shadow analyzer — X > 0

---

## Phase 2 — Solver Registration (1 day, parallel)

Register as an official CoW Protocol solver on Arbitrum One.
Once RouterSolver produces real solutions, flip from shadow mode to live submission.

**Prerequisites:**
- ⚠️ **Solver Bond**: CoW Protocol requires staking COW tokens as collateral to register.
  Amount and process: https://docs.cow.fi/cow-protocol/reference/core/auctions/bonding
  This may be a capital requirement — verify before committing to timeline.
- Solver endpoint (`/solve`) must be publicly accessible (current Hetzner setup qualifies).

**Code change**: One env var flip from shadow mode to live submission.

**Revenue starts here.** Weekly COW token payouts begin.

---

## Phase 3 — CoWJohnsonSolver: RF + Johnson's Cycle Finder (1–2 weeks)

### Problem
CoW ring trades (pure surplus, zero AMM cost) are missed because:
- The order graph has 1200 nodes — Johnson's algorithm is intractable at that scale
- `CoWMatchingSolver` currently finds 0 solutions (graph too noisy)

### Design

**Prerequisites for Phase 3:**
- `extract_features.py` must have run to populate `token_features` table (on-chain data per token)
- `train_classifier.py` must have produced a model in `/data/models/current.pkl`
- Requires sufficient labeled data: ≥ 100 auctions with winner data for cold-start RF
  (we have ~291 now — enough to start, improves over time)

**Step 1 — RF pre-filter** (existing `edge.classifier`):
```
For each unique token in auction:
    score = TokenClassifier.score(TokenFeatures[token])  # P(legit)
Keep orders where score(sell_token) ≥ 0.4 AND score(buy_token) ≥ 0.4
Result: ~1200 → ~50–150 orders
```

If no model loaded → use all orders (safe fallback).

**Step 2 — Graph construction:**
```
Nodes = unique tokens in filtered orders
Edges = filtered orders (sell_token → buy_token, weight = sell_amount)
```

**Step 3 — Cycle enumeration** (`networkx.simple_cycles`, max length = 4):
```
All simple cycles of length 2, 3, 4 in the filtered graph
```

**Step 4 — Profitability check per cycle:**
- Length 2: do limit prices overlap? `amount_out_A ≥ buy_amount_B AND amount_out_B ≥ buy_amount_A`
- Length 3–4: small LP to find clearing prices where all orders profit

**Step 5 — Solution assembly:**
Best non-overlapping profitable cycles → `Solution` with clearing prices and trades.

### Files
| File | Action |
|------|--------|
| `src/solver/cow_johnson.py` | New — `CoWJohnsonSolver` strategy class |
| `src/solver/orchestrator.py` | Add to chain after naive, before router |

### Chain position after Phase 3
```
NaiveSolver (6ms, oracle prices, always fallback)
CoWJohnsonSolver (RF filter → Johnson's → pure CoW rings)
RouterSolver (top-30 ETH value, real DEX prices)
```

### Risk
`CoWMatchingSolver` already finds 0 solutions in 30 Arbitrum auctions.
CoW rings may be rare on Arbitrum (high AMM activity, low order overlap).
Johnson's + RF is the best shot at finding them — but we only know after building it.

---

## Phase 4 — Accurate Scoring (1 week, can run in parallel)

Two improvements for meaningful shadow analytics:

### 4a — Winner-price comparison column
New DB column `score_vs_winner_prices_wei` on `shadow_solutions`.

In `persist_winner_and_outcomes`: after storing winner, extract `clearingPrices`
from `raw_solution`, recompute our surplus at those prices, UPDATE rows.

Answers: *"if we had executed at market prices, what would our score be?"*

### 4b — NaiveSolver price refinement
After naive finds trades (2–10 orders, 2–6 unique token pairs), run targeted
RouterSolver quotes for just those pairs (not all 1200 orders).
Replace oracle clearing prices with real DEX prices in the solution.

New file: `src/solver/price_refiner.py`
`NaiveSolver` gets optional `multicall` injection.

---

## Revenue Model

| Source | Mechanism | Timeline |
|--------|-----------|----------|
| COW token rewards | Win auctions, earn weekly protocol payout | Phase 2 |
| Solver surplus | Keep difference between limit price and execution price | Phase 2 |
| CoW ring surplus | Pure protocol surplus, no AMM cost | Phase 3 |
| JIT liquidity | Provide liquidity in solution, earn spread | Future |

---

## 6. Go/No-Go Gate — 1 week honest shadow (before any KYC spend)

**Why this gate exists.** Spec 2 Phase 1 is implemented. The Phase-2 exit gate in the
original design spec (≥5 % hypothetical win-rate) was met on bipartite CoW solutions
in isolation (33 % over 7 days, see `analyze_cow_rings --days 7` on 2026-05-23), but
that is a slice — only ~5 auctions/day match bipartite. Naive surplus in shadow is
inflated by oracle-price clearing and is not a valid signal. We need to know whether
**router-v2 with real DEX prices** is competitive on the *full* Arbitrum order flow
before committing to KYC, dedicated RPC, and the DAO onboarding queue.

**Gate window:** 7 calendar days starting 2026-05-24 (00:00 UTC).

**Tooling.** `scripts/analyze_router_solutions.py` produces the headline metric.

**Pass criteria (need ALL to start KYC):**

| # | Metric | Threshold | How measured |
|---|--------|-----------|--------------|
| G1 | Router-v2 solutions produced per day | ≥ 30 | `analyze_router_solutions --days 7` total / 7 |
| G2 | Router-v2 hypothetical win-rate (router-only auctions) | ≥ 10 % | same script, hypothetical-wins count |
| G3 | Bipartite-CoW hypothetical win-rate | ≥ 25 % | `analyze_cow_rings --days 7` (already at 33 %) |
| G4 | Median router-v2 CIP-14 score | ≥ 50 % of winner median | same script, delta column |
| G5 | Zero solver-side outages > 30 min (server, OOM, crash) | observed | `docker logs cow-solver --since 24h` daily check |

**Fail-fast triggers (stop early, don't wait full week):**

- After 48 h: < 5 router-v2 solutions/day → RPC throttle is fatal; need paid tier *before*
  the gate is meaningful
- Any day: solver crashes / OOM > 3× → stability blocker before KYC
- Day 3 check-in: bipartite win-rate dropped below 15 % → naive-Composer interaction
  regression, debug first

**On Pass:** start KYC flow (passport scan, Gewerbeschein), open Telegram contact with
CoW Team (t.me/cowprotocol), provision paid RPC tier, write Phase-2 plan for
CoWJohnsonSolver.

**On Fail:** archive the project as a learning investment. Document what would have
needed to change (likely: RF pre-filter for Johnson's, dedicated RPC, or different chain).
Do NOT proceed to KYC — sunk cost.

**Cost during gate:** €0 incremental (continue on free Alchemy + shared Hetzner).
**Decision lead time after Pass:** ~3 weeks until first Barn settlements.

---

## Prioritized Backlog

1. ✅ **RouterSolver order-cap + parallel quotes + ETH-value sort** — *done 2026-05-23*
2. ✅ **`analyze_router_solutions.py`** — *done 2026-05-23, gating tool*
3. 🎯 **Run 7-day honest shadow + measure G1–G5** — see §6 above
4. **Solver registration (KYC + bonding pool)** — Gated on §6 Pass
5. **Dedicated RPC tier (Alchemy paid / QuickNode)** — Gated on §6 Pass, ~€50–100/mo
6. **Run `extract_features.py` + `train_classifier.py`** — prerequisite for Phase 3 RF filter
7. **CoWJohnsonSolver** — 1-2 weeks, pure CoW surplus, only after §6 Pass
8. **Accurate scoring (4a + 4b)** — 4b shipped, 4a (winner-price comparison column) open
9. **JIT trades** — requires capital, future phase
