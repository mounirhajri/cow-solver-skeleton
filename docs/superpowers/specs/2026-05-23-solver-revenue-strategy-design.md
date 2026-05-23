# CoW Solver — Competitive Strategy & Revenue Plan

**Date:** 2026-05-23  
**Status:** Approved  
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

## Phase 1 — RouterSolver Order-Cap (1–2 days) 🎯 HIGHEST PRIORITY

### Problem
RouterSolver loops over all ~1200 orders and makes on-chain multicall quotes for each.
Per-strategy timeout = 13s ÷ 5 strategies = 2.6s. Always times out. Zero real solutions.

### Fix
Sort orders by ETH value (`sell_amount × reference_price`), take top N before the loop.

```python
# src/solver/router.py  — RouterSolver.solve()
orders = sorted(
    auction.orders,
    key=lambda o: _eth_value(o, auction.tokens),
    reverse=True,
)[:self._order_cap]   # default 30
```

Where `_eth_value` = `sell_amount * token.reference_price // 10**18` (pure math, no I/O).

### Files changed
| File | Change |
|------|--------|
| `src/solver/router.py` | `order_cap: int = 30` param, sort+slice before loop |
| `src/config.py` | `router_order_cap: int = 30` setting |
| `src/solver/orchestrator.py` | pass `settings.router_order_cap` to `RouterSolver` |

### Expected outcome
- RouterSolver finishes in ~200–500ms (30 orders × ~10ms per multicall batch)
- Produces solutions with **real DEX prices** — no oracle inflation
- `our_score_wei` becomes an accurate CIP-14 score
- Directly competitive with other solvers in the live competition

### Success metric
`router-v2: X/30 solved | p50 ~300ms` in shadow analyzer — X > 0

---

## Phase 2 — Solver Registration (1 day, parallel)

Register as an official CoW Protocol solver on Arbitrum One.
Once RouterSolver produces real solutions, flip from shadow mode to live submission.

**Revenue starts here.** Weekly COW token payouts begin.

---

## Phase 3 — CoWJohnsonSolver: RF + Johnson's Cycle Finder (1–2 weeks)

### Problem
CoW ring trades (pure surplus, zero AMM cost) are missed because:
- The order graph has 1200 nodes — Johnson's algorithm is intractable at that scale
- `CoWMatchingSolver` currently finds 0 solutions (graph too noisy)

### Design

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

## Prioritized Backlog

1. ✅ **RouterSolver order-cap** — 1-2 days, immediate revenue impact
2. ✅ **Solver registration** — 1 day, parallel with above
3. **CoWJohnsonSolver** — 1-2 weeks, pure CoW surplus
4. **Accurate scoring (4a + 4b)** — 1 week, analytics quality
5. **JIT trades** — requires capital, future phase
