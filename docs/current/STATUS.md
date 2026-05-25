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

Post-fix verification (2026-05-25 15:00 UTC): all 5 strategies persist rows, 0 timeouts in sample window.

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

### 3.2 Multi-Party Same-Ring Loop
**Symptom in logs:** Same UID `0xd9ec0e2f...` dropped as `multi_party_ring_dropped_non_partial_short` in every auction
**Root cause:** A persistent TWAP wallet submits the same order pattern; Multi-Party finds it as a viable ring but the order is `partiallyFillable=false` and the LP can only fill it short
**Implication:** Multi-Party's structural "no_solution" rate ≈ 100% on dense Arbitrum auctions until partial-fills support lands

### 3.3 Bipartite Wins Are Volume-Floor Wins
**Observation from 30d data:**
- 100% of bipartite wins are against winner_solver scores < 0.001 ETH
- 9/10 sample wins are against kaisersolver-solve, scoring ~0.000014 ETH (volume-floor strategy)
- We do NOT beat helixbox/rizzolver/sector on high-value auctions
**Implication:** Pitch story is "we compete on stablecoin CoWs that top-tier solvers ignore," not "we beat the leaders."

### 3.4 Bipartite Coverage is Narrow
- 60% of auctions attempted, but only 10% produce a solution (no_solution rate 91%)
- 98.5% of solutions are on USDC/USDT pair (130/132 historical)
- 2/132 are WETH/USDC

---

## 4. Realistic Revenue Expectations

**Important:** Earlier Memory/spec projections of €1090-2600/mo NET were based on assumed win-volume that did not materialize in verified shadow data.

**Verified base case (bipartite-only, 30d historical):**
- 2160 auctions/month, 130 bipartite solutions, 64 hypothetical wins
- Surplus: 64 × 0.0003 ETH × €1827 ≈ €35/mo
- Performance reward: 64 × 0.00024 ETH × €1827 ≈ €28/mo
- Gross: ~€63/mo, minus 15% bonding fee, minus €60 server = **~€-1/mo NET (break-even)**

**Realistic 3-month range (Barn phase):**
- Pessimistic: €-20 to €0/mo (bipartite-only)
- Realistic: €0 to €40/mo (bipartite + router-v2 if it stabilizes at 20-30% win-rate)
- Optimistic: €60 to €150/mo (if multi-party score-inflation gets fixed and rings settle)

**Pitch implication:** Don't lead with revenue numbers. Lead with technical depth + honest telemetry + niche positioning (stablecoin CoW specialist).

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
