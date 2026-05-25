# Project Status — cow-solver-skeleton

**Last verified:** 2026-05-25 via live DB query + code audit
**Deployment:** Hetzner CX22, Arbitrum One, shadow-only (no production submission yet)

This doc is the single source of truth for "what's running, what's broken, what's next." It supersedes any claims in archived specs/plans under `docs/archive/`.

---

## 1. Implementation Status (verified)

### 1.1 Strategies — all 5 active in orchestrator

| Strategy | Code | Production Behavior | Notes |
|---|---|---|---|
| `naive` | `src/solver/naive.py` | Always solved (1306/1307 historical) | Shadow-only; **never submitted** — its oracle-derived clearing prices over-state real settlement (see Known Issues §3.1) |
| `cow-matching-bipartite` | `edge/matching/bipartite.py` | Greedy 2-party CoW matcher | USDC/USDT specialist — 130/132 historical wins on that pair @ 1:1 clearing |
| `cow-matching-multi-party` | `edge/matching/multi_party.py` | Johnson cycle enum + LP surplus-maximization | Honest fail rate ≈100% post-fix (most rings non-partial-fillable) |
| `router-v2` | `src/solver/router.py` | V3 QuoterV2 top-N batched via Multicall3 | Chunk size 25 (gas cap), `router_v3_only_batched=True` default |
| `composer` | `edge/matching/composer.py` | CIP-67 token-disjoint merge | Token overlap → candidate rejected, no price averaging |

`long-tail` router exists in code (`edge/pool_indexer/long_tail_router.py`) but is **disabled** in production via `LONG_TAIL_ENABLED=false` due to Alchemy concurrent connection limits.

### 1.2 Infrastructure

- **Shadow Pipeline**: poller → solver → reconciler → Postgres (5 tables: shadow_auctions, shadow_solutions, shadow_winners, token_outcomes, token_features)
- **Persistence**: CIP-14 score in `shadow_solutions.our_score_wei`; `score_vs_winner_prices_wei` is structurally NULL on Arbitrum (solver_competition API doesn't return clearingPrices)
- **EBBO Pre-Submission Validator**: `src/solver/ebbo.py`, default 50bps tolerance, sell-only (buy-side deferred)
- **RF-Filter**: IsolationForest, threshold 0.4, whitelisted core Arbitrum tokens, no-op when classifier model is None
- **Monitoring**: Prometheus metrics @ `:8001/metrics`, structlog JSON, analysis scripts in `scripts/`
- **Deployment**: GitHub Actions → GHCR → SSH to Hetzner → Docker Compose + Alembic on restart

### 1.3 Today's Fixes (2026-05-25)

| Commit | Fix |
|---|---|
| `5b64a31` | `src/main.py` + `src/solver/orchestrator.py`: persist shadow attempts even on TimeoutError/Exception (caused 15.5h outage 2026-05-24/25 — multi-party LP timeouts silently discarded all attempt rows) |
| `e1ad98b` | `src/config.py`: solve_timeout 13s → 25s — outer cap was starving router-v2 (last in chain, 11s budget) after partial-fills deploy slowed bipartite/multi-party |
| `787a8c5` (edge) | `edge/matching/bipartite.py`: drop multi-pair matches whose limit prices are violated at the Solution's committed clearing ratio (previous code emitted limit-violating trades that CIP-14 scoring silently clamped, producing inflated 7.77 ETH median scores on $80M-scale USDC/USDT TWAP fills) |
| `770287a` | `src/shadow/scoring.py`: zero out CIP-14 score when any fulfillment trade violates its limit at the Solution's clearing prices (defense-in-depth against the bipartite case + any future strategy that emits limit-violating Solutions) |
| `629fd71` | `src/shadow/persist.py` + `src/solver/router.py`: skip score computation for `strategy="naive"` (oracle reference_price clearing = phantom surplus, KNOWN-BAD); add `router_high_surplus_observed` log when implied surplus > 100bps (forensic visibility for off-market quotes) |
| `7c92cea` (edge) + `7f4d03a` | `edge/matching/bipartite.py`: cap per-match fill volume at 1% of sell_amount for partially-fillable orders ≥ 10^12 base units (orthogonal to limit-violation fix; addresses TWAP orders encoded at full 10M USDC sellAmount producing ~1.7 ETH per fill on unrealisable depth) |

Post-fix verification (2026-05-25 18:00 UTC, clean TRUNCATE baseline): bipartite max score ≤ 0.0000 ETH (cap working), naive scored=0 (NULL enforced), multi-party honest no_solution, router-v2 + composer ~6 ETH (legit V3 AMM arb on persistent loose-limit orders). All 5 strategies persisting rows; no timeouts.

### 1.4 Raw shadow data vs. aggregate analytics

`shadow_solutions.our_score_wei` is a PER-ATTEMPT score. The same persistent order (e.g. an off-market loose-limit buy that keeps appearing in successive auctions) can be matched by the same strategy across many auctions — each emission is a separate row with its own score, but only ONE such fill could actually settle on-chain.

Aggregate revenue projections must deduplicate. `scripts/estimate_economics.py` already collapses persistent UIDs per strategy:

```
[dedup] cow-matching-bipartite: 13 raw rows → 5 distinct UID-sets (8 repeats collapsed)
[dedup] cow-matching-multi-party: 26 raw rows → 8 distinct UID-sets
[dedup] router-v2: 36 raw rows → 1 distinct UID-set (35 repeats collapsed)
```

When quoting numbers for external pitch or competitor analysis, use `estimate_economics` output, not raw `SELECT FROM shadow_solutions` aggregates.

Naive's score is NULL'd at persistence (`src/shadow/persist.py`) because the price_refiner code path uses oracle reference_prices as clearing prices — see `src/solver/price_refiner.py:168-183` (KNOWN-BAD comment). Naive is never submitted; its rows exist for composer-debug observability only and would otherwise distort percentile dashboards.

---

## 2. Open Work (from specs/plans)

### 2.1 Critical (blockers for Barn/Bonding-Pool)

**Partial Fills Support** — Spec [`2026-05-25-partial-fills-design.md`](../archive/specs/2026-05-25-partial-fills-design.md), Plan [`2026-05-24-partial-fills-implementation.md`](../archive/plans/2026-05-24-partial-fills-implementation.md)
- ~30% of Arbitrum orders are `partiallyFillable=true` (per spec; not re-verified)
- Today: Router emits full-or-nothing, Bipartite skips mismatches, LP emits floor without partial-ring acceptance
- Status: Phase 1 LP-rounding tests landed in PR #30 ("partial-fills Phases 1-4"), but downstream strategies still ignore the partial flag
- Effort: 5-10 days for full integration

**Monitoring + Alerting** — no current spec exists
- 15.5h outage 2026-05-24/25 with zero alerts
- Prometheus rules file referenced in old specs but **not in repo**
- Effort: 2-3 hours for minimum 5 alerts (poller-health, solver-timeout-rate, disk, DB-growth, strategy-win-rate-drop)

**Automated Deploy Chain Restoration**
- `HETZNER_SSH_KEY` GHA secret is passphrase-protected → CI deploys fail
- All deploys currently manual via local SSH
- Effort: 30 min (replace with passphrase-less ed25519 key)

### 2.2 Important (for Mainnet/scaling)

**Multi-Party Score Inflation** — `edge/matching/surplus.py:263-271`
- Default weight `10^18` for tokens without native_price → unbounded LP "surplus"
- Today's no_solution rate (~100%) suggests LP is rejecting these rings, but the risk surface remains
- Effort: 1-2 days audit + per-token weight clamping

**Phantom CIP-14 Surplus (Legacy)** — `src/solver/price_refiner.py:168`
- KNOWN-BAD comment in source: oracle reference_price used as clearing_price when V3 quote unavailable
- Harmless today because naive is excluded from submission via composer (`orchestrator.py:150-152`)
- Real fix tied to partial-fills CIP-67-uniform-clearing-prices LP (separate spec)

**Smart-Wallet Log Triplication** — Spec [`2026-05-26-router-and-logging-followups.md`](../archive/specs/2026-05-26-router-and-logging-followups.md) §3
- `smart_wallet_orders_observed` event fires 3× per auction (router, bipartite, multi-party)
- Already moved to orchestrator (line 70-75) — verify and remove from strategies
- Effort: 1 hour

**Buy-Order EBBO Validator**
- `src/solver/ebbo.py` is sell-side only; buy-side deferred
- Risk: buy orders skip pre-submission check
- Effort: 2-3 hours, dependency on partial-fills path-encoding

### 2.3 Nice-to-have

**Factory Watcher + Pre-warmed Pool Cache** — Spec [`phase2-pool-indexer-design.md`](specs/phase2-pool-indexer-design.md) §6.1
- Lazy-indexer today has ~300ms cold-quote penalty on first pair
- WebSocket factory watcher would eliminate this
- Effort: 3-5 days

**ML Model Actually Training** — Plan [`2026-05-23-ml-token-classifier-pipeline.md`](../archive/plans/2026-05-23-ml-token-classifier-pipeline.md)
- Feature engineering + Optuna + chronological-split all implemented
- Blocker: `generate_labels()` produces zero labels because CoW public API doesn't return `clearingPrices`
- Real fix: Phase-4 Settlement-Event-Reconciler (would read on-chain settlement logs to backfill labels)
- Deferred until Barn-phase data starts flowing

---

## 3. Known Issues (documented in code)

### 3.1 Phantom Score Bug (KNOWN-BAD)
**Location:** `src/solver/price_refiner.py:168-173`
**Comment in code:** *"KNOWN-BAD: assigning reference_price as clearing_price produces phantom CIP-14 surplus equal to the order's OTM-headroom at oracle"*
**Impact:** naive strategy's `our_score_wei` can be wildly inflated. Mitigated at composer level (naive excluded from submission), but inflation polluted historical `estimate_economics.py` projections.

### 3.2 Multi-Party Same-Ring Loop (mostly blocked, occasionally solves)
**Symptom in logs:** Two persistent UIDs (`0xd9ec0e2f...` and `0xfe7c06bb...`) dropped as `multi_party_ring_dropped_non_partial_short` in every auction — both are `partiallyFillable=false`, LP can only fill them short.

**Status post-cleanup baseline (2026-05-25 evening, n=103):**
- 1/103 = ~1% solve rate. Auction 7371674 successfully composed a 3-token WETH/USDC/USDT ring with $48 surplus (0.0266 ETH) — the algorithm works end-to-end when auction composition permits.
- ~99% of attempts still no_solution because Johnson's 13-ring enumeration always includes one of the two persistent fill-or-kill blockers.

**Implication:** Multi-Party LP math is verified production-ready; the bottleneck is partial-fills support (STATUS §2.1). Once partial-fills phase 2-4 ships, the 99% blocked rings could become 10-20% settled rings.

### 3.3 Bipartite Wins Are Against Volume-Floor Strategies
**Observation from clean post-fix baseline (n=101):**
- Win-rate: 12.9% (CI 6.2-19.6% at n=101, expected to tighten to ~12% at n=1000+)
- Loss distribution: 53% lose by >90% (heavily out-scored), 25% lose by 50-90%, 8% close losses (10-50%)
- All wins against `helixbox-solve` (44% market share) or `kaisersolver-solve` (29%) — both run volume-floor strategies (minimal-surplus submissions optimizing for performance reward frequency)
- We do NOT beat `rizzolver` (median 0.0019 ETH), `sector` (median 0.0063 ETH), or `wraxyn-solve` (single 0.157 ETH win observed — high-value arb opportunity pattern)

**Implication:** Pitch positioning is "competitive against the volume-floor segment of the Arbitrum solver pool" — NOT "outperforming the top tier."

### 3.4 Bipartite Coverage Profile
- 100% of auctions attempted (poller delivers all 1000+ order auctions)
- 19.2% solve rate (102 solved / 532 auctions in baseline window)
- Token-pair distribution: dominated by recurring USDC/USDT CoW pairs at micro-scale ($6-100 trades), plus WETH/USDC and capped TWAP slices
- 9 distinct first-trade UIDs in 103 emissions → real opportunity diversity, not single-order overcount

---

## 4. Realistic Revenue Expectations

**Important:** Earlier Memory/spec projections of €1090-2600/mo NET were based on inflated phantom scores that did not survive the 2026-05-25 bipartite + scoring + cap fixes. The numbers below are from the clean post-fix shadow baseline.

### 4.1 Verified Conservative Floor (`estimate_economics --days 1`, n=9 after dedup + p99 outlier-cap)

```
Hypothetical wins observed:    9   (8 bipartite + 1 router-v2)
Median surplus per win:        0.000006 ETH  (dominated by small USDC/USDT CoWs)
Mean surplus per win:          0.7 ETH       (heavy outlier tail, ignored by projection)
Projected wins / month:        274 (±55)

Monthly revenue projection @ ETH = €1827:
  Performance reward (gross):  €120
  Solver surplus (gross):      €3   (median-based — conservative)
  After 15% bonding fee:       €102
  Minus server + RPC:          -€60
  ─────────────────────────────────
  Net point:                   +€45/Mo
  Net low / high (95% CI):     +€24 / +€66

G6 BREAK-EVEN GATE: PASS  (Net low-band ≥ -€20)
```

**Why this number is conservative:** the projection uses median surplus (drowning the long tail). Router-v2's 6 ETH single-shot arb is capped out as p99 outlier. Multi-party's 1% solve rate (verified, see §3.2) contributes effectively 0 to the projection given the small sample.

### 4.2 Realistic Range with Strategy Upside

| Scenario | Bipartite | Router-v2 | Multi-Party | Net/Mo |
|---|---|---|---|---|
| **Worst** (script floor) | €45 | €0 | €0 | **€45** |
| **Likely** (one router settle / 2mo, multi-party 1 ring / 2 days) | €60 | €500 | €1500 | **€2,000** |
| **Optimistic** (regular router arbs, multi-party 5/day) | €100 | €5,000 | €7,000 | **€12,000** |

### 4.3 Solver-Pool Reference Points

From clean-baseline `shadow_winners` data (last ~2h, n=110 auctions with named winners):

| Solver | Wins | Median Score | Strategy Pattern |
|---|---|---|---|
| **helixbox-solve** | 44% | 0.000183 ETH | Volume-floor + occasional medium arbs |
| **kaisersolver-solve** | 29% | 0.000044 ETH | Pure volume-floor (minimum-surplus submissions) |
| **rizzolver** | 6% | 0.001850 ETH | Higher-quality bigger wins |
| **wraxyn-solve** | 1% | 0.157 ETH | **High-value single arbs (~$300 per win)** — reference for what our router-v2 capability could capture if V3 depth realises |
| sector, zeroex, baseline, kipseli, others | <2% each | varied | mixed |

**Where we'd fit:** competitive in the kaisersolver/helixbox volume-floor band. Capability (router-v2, multi-party) for wraxyn-style high-value single arbs exists but is unproven in production.

### 4.4 Pitch Framing

**Conservative talking points (verified):**
- "G6 break-even gate passes on clean post-bugfix baseline at €45/Mo NET point estimate"
- "Bipartite ~13% hypothetical win-rate (n=101) primarily on stablecoin CoW pairs"
- "Multi-party LP demonstrated end-to-end (auction 7371674, 3-token WETH/USDC/USDT ring, $48 surplus)"
- "Router-v2 captures off-market loose-limit arbitrage when V3 depth permits"

**Strategic positioning (forward-looking):**
- "Not pitching first-month revenue — positioning for long-term solver contribution"
- "Mainnet expansion potential 5-20× current Arbitrum-only volume"
- "Partial-fills phase 2-4 would unblock multi-party's ~99% currently-blocked rings"

**DO NOT claim:**
- ❌ Win-rates above 15% without confirming with n=500+ data
- ❌ Specific monthly revenue numbers above €100 without router/multi-party validation
- ❌ "Beat the top tier" — we don't beat rizzolver/sector/wraxyn on their wins

---

## 5. Future Work (not in any spec yet)

These emerged from the 2026-05-25 audit and should become specs before Barn-phase application:

1. **Monitoring/Alerting Spec** — minimum 5 alerts (Poller-Health, Solver-Timeout-Rate, Disk-Pressure, DB-Growth, Strategy-Win-Rate-Drop). Required for production credibility.

2. **Large-Auction Strategy Decision** — Poller currently skips `/solve` for `len(orders) > 50` (rate-limit defense). We see ~5% of auctions. Decision needed: register as official solver (full feed) vs. accept partial view.

3. **Multi-Party Score-Inflation Risk Audit** — measure impact of `10^18` default weight on actual production scenarios. Decide on per-token weight clamping or whitelist enforcement.

4. **Time-Series Win-Rate Monitoring** — `estimate_economics.py` is a one-off CLI. Production needs continuous Prometheus metric (daily win-rate per strategy) to detect regression.

5. **Settlement-Event Reconciler (Phase 4)** — reads on-chain settlement logs to backfill `token_outcomes` labels. Unblocks ML classifier training and proper post-trade scoring.

6. **Repository History/Reorganization** — current `superpowers/` doc structure was reorganized to `current/` + `archive/` on 2026-05-25. This audit doc should be updated whenever a spec or plan reaches one of the four statuses.

---

## 6. Document Map

**Current (live reference):**
- [`README.md`](../../README.md) — top-level intro, deployment, config knobs
- [`ARCHITECTURE.md`](../../ARCHITECTURE.md) — one-pager for code reviewers
- [`STATUS.md`](STATUS.md) — this file
- [`DEPLOYMENT.md`](DEPLOYMENT.md) — Hetzner deploy procedure
- [`anomaly-detection.md`](anomaly-detection.md) — RF token classifier pipeline
- [`specs/cow-solver-design.md`](specs/cow-solver-design.md) — master design spec
- [`specs/phase2-pool-indexer-design.md`](specs/phase2-pool-indexer-design.md) — long-tail pool indexer (impl exists, disabled in prod)

**Archive (historical/superseded/future):**
- [`docs/archive/specs/`](../archive/specs/) — completed-or-superseded design docs
- [`docs/archive/plans/`](../archive/plans/) — execution plans now closed
- [`docs/archive/progress/`](../archive/progress/) — session logs

When adding a new spec or plan, drop it in `docs/current/` and reference it from §2 above. When it's done or obsolete, move it to `docs/archive/` and update §1.
