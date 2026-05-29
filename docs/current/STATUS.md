# Project Status — cow-solver-skeleton

**Last verified:** 2026-05-29 via validate_data.py + live shadow DB
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
| `router-v2` | `src/solver/router.py` | V3 QuoterV2 top-N batched via Multicall3 | `ROUTER_MAX_ORDERS=4` (PublicNode gas-simulation cap), env-override via docker-compose |
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

### 1.4 Dynamic Ghost-Order Detection (2026-05-26)

**Discovery:** the clean post-TRUNCATE baseline above was **99.4% ghost-polluted**. SQL forensics (n=524 bipartite solutions, 7d window) found:
- 100% ghost trades: 100 solutions (19.1%)
- Mixed (≥1 ghost): 421 solutions (80.3%)
- Ghost-clean: 3 solutions (0.6%)

A "ghost-order" is a CoW orderbook order that no live solver ever settles — typically EIP-1271 contracts that reject signature validation at settle-time, abandoned bots, or addresses with broken approval/balance.  CoW Protocol broadcasts them to all solvers regardless; real solvers filter via onchain simulation.  Our paper-trading bipartite matcher matched against them, producing CIP-14-valid scores that no AMM could realise.

**Effect on prior baseline numbers:**
- Avg score ghost-involved: 0.005088 ETH
- Avg score ghost-clean: 0.000005 ETH (1000× lower)
- → claimed "median 0.008 ETH" was ~99% ghost-driven
- → claimed "12.9% bipartite win-rate (n=101)" cannot be reproduced on ghost-clean data (true n=3, statistically meaningless)
- → §4.1 `estimate_economics` €45/Mo NET ran on polluted data — needs re-evaluation after detector rolls out

**Fix shipped 2026-05-26:**

| Commit | Layer | What |
|---|---|---|
| `7c4ad9e3b821` | migration | new table `ghost_orders` (uid PK, owner, sell/buy_token, n_auctions_seen, first/last_seen_at, detected_at, last_refreshed_at) |
| (this commit) | script | `scripts/refresh_ghost_set.py` — detects UIDs seen ≥20 in 24h with 0 winner-settlements in 7d; upserts to `ghost_orders`; self-corrects false-positives by re-evaluating each cycle; stale-cleanup after 14d |
| (this commit) | sidecar | `docker-compose.yml` — new `ghost-refresh` container, `--loop` mode, 30-min interval |
| (this commit) | detector | `edge/matching/ghost_detector.py` — `DynamicGhostDetector` with 5-min TTL cache, graceful fallback on DB errors |
| (this commit) | matcher | `edge/matching/bipartite.py` — injects ghost-detector filter BEFORE static `GHOST_OWNER_BLACKLIST` (kept as defense-in-depth) and RF-filter |
| (this commit) | orchestrator | `src/solver/orchestrator.py` — wires `DynamicGhostDetector(session_factory)` into both `BipartiteMatcher` instantiation points |

**False-positive analysis on detection rule (`seen ≥ 20 in 24h, 0 settled in 7d`):** verified at 0.8% empirically (10 settled UIDs of 1257 candidates).  18 of those 23 settlement-FPs sit at 100+ visibility — irreducible slow-fill institutional orders that no visibility-based heuristic can separate from ghosts.  The detector's self-correction step removes these from `ghost_orders` on the next refresh cycle after they settle, bounding their FP-lifetime to ~30 min.

**Conceptual gap that remains:** the matcher still doesn't run onchain simulation, which is what real solvers (helixbox, kaisersolver, wraxyn) use to deterministically catch ghosts.  Phase B will add `eth_call`-based pre-flight before going live — needed regardless of the dynamic detector because settlement TXs that revert burn gas.

**Live verification (2026-05-26 ~11:00 UTC, ~1h after deploy):**
- Per-auction filter: `n_filtered ≈ 1106` constant across logged events → **~99.1% of all orders per auction are ghosts**
- `n_remaining ≈ 10` real sell-orders per auction for bipartite to match against
- Aligns with prior 99.4% pollution finding (now measured on order-count basis vs solution basis)

### 1.5 Multi-Party + Phase A Interaction (2026-05-26)

**Earlier hypothesis in this section was wrong — corrected below after deep inspection of the actual ring contents.**

**Original hypothesis (now retracted):** Phase A removes ghosts from Multi-Party input → rings built from real orders → first scored post-deploy solve (auction 7379300, 0.006 ETH) is evidence Phase A unlocked ~€5,000/Mo Multi-Party revenue.

**What the deep inspection actually found:**

Auction 7379300's "post-Phase-A" 3-trade ring (3-token cycle through `0x2f2a25` / USDC / USDT) **contains UID `0x9bed2dd21329…` as one of its three legs**.  Owner address `0x0c3f65b68b3059f7c02d8be083a885fe67d7b6cd` is identical to the original USDC↔USDT ghost identified at the start of the investigation, and the UID is currently in `ghost_orders` (seen=618).

**Root cause:** when wiring Phase A on 2026-05-26, the `DynamicGhostDetector` was only injected into `BipartiteMatcher`, not into `CoWMatchingSolver` (Multi-Party).  See [src/solver/orchestrator.py](src/solver/orchestrator.py) at the time — Multi-Party constructor lacked the `ghost_detector` parameter and the call site didn't pass one.  So Multi-Party continued to operate on the un-filtered order set.

**What this means:**

| Window | Multi-Party solved/total | Rate | What's actually true |
|---|---|---|---|
| 6-24h ago (pre-PR-#34) | 1/416 | 0.24% | One real solve (auction 7371674, verified 3-token ring) |
| 1-6h ago (post-PR-#34, pre-Phase-A) | 7/180 | 3.89% | Rings found but ghost-polluted → zero-volume guard NULL'd scores |
| 0-1h ago (no Phase A applied to Multi-Party) | 1/37 | 2.70% | Ring still includes ghost UID; CIP-14 score likely phantom (would revert on settle) |

**Re-interpretation of Multi-Party's true performance:**
- Verified scored solves on clean data: **n=1** (auction 7371674, 0.0266 ETH, 2026-05-25 evening)
- Post-PR-#34 ring emissions: high rate (~3-4%) but content is suspect — guards NULL most, the one that wasn't NULL'd contains a known ghost
- Until Multi-Party is also wired to Phase A and we re-sample, the actual ghost-clean solve rate is unknown

**Fix shipped (this commit):**

| File | Change |
|---|---|
| `edge/matching/multi_party.py` | `CoWMatchingSolver.__init__` gains `ghost_detector` parameter; `solve()` runs the dynamic-ghost filter BEFORE ring-cooldown / RF-filter / OTM-graph construction |
| `src/solver/orchestrator.py` | passes `ghost_detector=ghost_detector` to both `CoWMatchingSolver` instantiations (parallels the BipartiteMatcher wiring) |
| `tests/test_edge/test_multi_party.py` | 2 new tests for the ghost-filter integration (analogous to bipartite tests) |

**What we still need to verify (next 6-24h):**
- After deploy: do new Multi-Party solves still contain ghost UIDs?  Should be zero.
- Does the solve rate drop from ~3-4% to a lower-but-genuine rate?  Likely yes — many of the 3-4% rings were enabled by ghost legs.
- What's the true ghost-clean Multi-Party rate and surplus?  Currently unknown.

**Revised expectation for Multi-Party revenue:** unknown.  The ~€5k/Mo projection from the retracted hypothesis was based on assuming the lone post-deploy scored solve was genuine.  It wasn't.  Real number could be anywhere from €0/Mo (if ghost-clean solves are rare) to €5k+/Mo (if PR #34 found genuine new ring topology beyond what ghosts were enabling).  24h sample required.

**Lessons from the discovery:**
1. End-to-end content verification (tracing every UID in a solve) beats aggregate metric checking — the score+rate signals looked encouraging, but the ring contents told the truth.
2. Phase A's success on Bipartite was real (verified separately); the gap was the orchestrator wiring, not the detector design.
3. Don't post hypotheses from n=1 — even if "directionally consistent" with prior models.  This was a self-inflicted error in the prior STATUS update.

### 1.6 Raw shadow data vs. aggregate analytics

`shadow_solutions.our_score_wei` is a PER-ATTEMPT score. The same persistent order (e.g. an off-market loose-limit buy that keeps appearing in successive auctions) can be matched by the same strategy across many auctions — each emission is a separate row with its own score, but only ONE such fill could actually settle on-chain.

Aggregate revenue projections must deduplicate. `scripts/estimate_economics.py` already collapses persistent UIDs per strategy:

```
[dedup] cow-matching-bipartite: 13 raw rows → 5 distinct UID-sets (8 repeats collapsed)
[dedup] cow-matching-multi-party: 26 raw rows → 8 distinct UID-sets
[dedup] router-v2: 36 raw rows → 1 distinct UID-set (35 repeats collapsed)
```

When quoting numbers for external pitch or competitor analysis, use `estimate_economics` output, not raw `SELECT FROM shadow_solutions` aggregates.

Naive's score is NULL'd at persistence (`src/shadow/persist.py`) because the price_refiner code path uses oracle reference_prices as clearing prices — see `src/solver/price_refiner.py:168-183` (KNOWN-BAD comment). Naive is never submitted; its rows exist for composer-debug observability only and would otherwise distort percentile dashboards.

### 1.7 Live-Diagnose & Fixes 2026-05-29

#### Router-v2 Out-of-Gas (FIXED — PR #57–#60)

`router_max_orders` was 9. With `router_v3_only_batched=True`, 9 orders × ~9 V3 quotes = ~81 Multicall3 entries → ~16M gas → PublicNode returns `-32000 out of gas` on every `eth_call` → 0% solve rate.

Fix: `router_max_orders = 4` (config default + docker-compose env override). 4 orders × ~9 calls = ~36 entries → ~7M gas, safely under cap. Result: solve rate 0% → 45–57% post-fix.

#### Env-Propagation Bug (FIXED — PR #57–#60)

`deploy/hetzner/docker-compose.prod.yml` was missing `${VAR}` entries for `LONG_TAIL_ENABLED`, `POOL_CACHE_TTL_SECONDS`, `POOL_CACHE_MAX_ENTRIES`, `AMM_STATE_LAZY`, `ROUTER_MAX_CONCURRENT`, `EBBO_ENABLED`, `EBBO_TOLERANCE_BPS`. Settings never reached the container — defaults always applied.

Also changed defaults: `long_tail_enabled=False` (was True: consumed ~90% of RPC budget on 0/711 solved), `pool_cache_ttl_seconds=300` (was 60: every auction flushed the cache).

#### Composer CIP-14 Surplus Fix (FIXED — PR #59–#60)

`edge/matching/composer.py:_estimate_surplus` sums raw `executed_amount` across heterogeneous tokens — decimals-blind (0.001 WETH at 10^15 base units beats 100 USDC at 10^8). Fix applied at orchestrator layer: pre-compute CIP-14 score via `src/shadow/scoring.compute_solution_score` for each candidate before calling `compose()`. Passing a positive `surplus_estimate` bypasses the broken fallback.

#### Live Validation 2026-05-29 (12h window, `validate_data --hours 12`)

| Strategy | n solved | Score range | Sanity |
|---|---|---|---|
| router-v2 | 235 | 0.0000073–0.246 ETH | ✓ |
| cow-matching-bipartite | 12 | 0.0000035–0.009 ETH | ✓ |
| composer | 8 | 0.0000064–0.008 ETH | ✓ |
| naive | 444 | NULL (all) | ✓ correct — never submitted |

All JSON fields present (prices + trades), no orphaned auction_ids, no negative/overflow scores.

#### Bimodal Performance Profile (ongoing)

Win-rate analysis (SCHICHT 3d, 12h window) shows bimodal distribution:
- Buckets 1–3 (micro/klein/mittel, <0.01 ETH): decent coverage + win-rate
- Bucket 4 (groß, 0.01–0.1 ETH): 76% of Surplus-Volumen, only 24% Coverage, 0% Win-Rate
- Root cause: `_expected_surplus_sort_key` de-prioritises whale orders; `ROUTER_MAX_ORDERS=4` limits exposure to large auctions

Fix options: extend `intermediate_tokens` (WETH+USDC+USDT+WBTC) or change sort-key to weight by `headroom × log(eth_value)`.

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

### 3.5 Bimodal Win-Rate / Bucket-4 Gap

**Observation (2026-05-29, 12h shadow data):** Bucket 4 ("groß", 0.01–0.1 ETH winner scores) contains ~76% of total Surplus-Volumen but we have only 24% Coverage and 0% Win-Rate there.

**Root cause:** `router._expected_surplus_sort_key` intentionally de-prioritises large orders (guards against over-filling TWAPs), combined with `ROUTER_MAX_ORDERS=4` limiting the number of top-N orders considered per auction.

**Impact:** We are effectively blind to the highest-value auctions. G6 economics pass only on the small-bucket floor.

**Proposed fix (not yet implemented):** Change sort-key to `headroom × log(eth_value)` or extend `intermediate_tokens` list to include WBTC for additional routing paths.

---

## 4. Realistic Revenue Expectations

**Important:** Earlier Memory/spec projections of €1090-2600/mo NET were based on inflated phantom scores that did not survive the 2026-05-25 bipartite + scoring + cap fixes. The numbers below are from the clean post-fix shadow baseline.

**Pending re-evaluation (2026-05-26):** the figures in §4.1–4.2 were computed BEFORE the ghost-detector deploy (see §1.4) — meaning 99.4% of bipartite contributions came from non-fillable ghost-pairs.  Once the detector has run for 24+ hours on production data, `estimate_economics` should be re-run.

Re-evaluation expectations after the full Phase A wiring (see §1.5 — Multi-Party was initially mis-wired, fixed in follow-up PR):
- Bipartite floor likely drops significantly (8 of 9 attributed wins were ghost-pollution)
- Multi-Party: unknown.  Memory's "1% solve rate" was outdated, PR #34 raised emission rate to ~3-4%, but inspection of post-deploy solves found ghost UIDs still in the rings — Multi-Party wasn't actually filtered until the follow-up PR.  True ghost-clean Multi-Party rate and surplus require 24h sample after the wiring fix.
- Router-v2 unchanged (independent of ghosts)
- Net effect: direction uncertain until full clean baseline lands.  No confident projection can be made today.

### 4.1 Verified Conservative Floor (`estimate_economics --hours 8`, post-fix data, n= ~20 after dedup)

Post-router-fix (2026-05-29, after `router_max_orders` 9→4):

```
Hypothetical wins observed:  ~20   (mostly router-v2 + bipartite)
Median surplus per win:       ~0.00025 ETH  (router-v2 median from validate_data)
Projected wins / month:       ~600 (±120)   (based on ~45% solve rate × auction frequency)

Monthly revenue projection @ ETH = €2,700 (approx):
  Performance reward (gross):  ~€195
  Solver surplus (gross):      ~€40
  After 15% bonding fee:       ~€166
  Minus server + RPC:          -€60
  ─────────────────────────────────
  Net point (approx):          +€146/Mo
  Net low / high:              +€117 / +€175

G6 BREAK-EVEN GATE: PASS
```

**Caveat:** window is 8h post-fix. Run `estimate_economics --hours 24` after collecting 24h clean data for a stable projection.

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
