# Overnight Execution Summary — Phases 0' + 1 + most of 2

**Start:** 2026-05-22 ~22:30 UTC
**End (this snapshot):** 2026-05-23 ~00:15 UTC

## Tasks completed (in commit order)

### Phase 0' Stabilization

| Task | Commit | Note |
|---|---|---|
| 0.1 Reconciler healthcheck | `7e523dd`, `40bb7bc` | File-based liveness pattern in `scripts/liveness.py` |
| 0.2 Shadow poller backoff | `d901920`, `4691361`, `02c059d`, `df1cd91` | Exponential backoff, large-auction skip, `RateLimitedError` |
| 0.3 pool_indexer stub | `9b3553a` (skel) + `e5163d7` (edge) | Unblocks orchestrator edge loading |

### Phase 1

| Task | Commit | Note |
|---|---|---|
| 1.1 Postgres schema | `ce9e371` | 5 tables: shadow_auctions, shadow_solutions, shadow_winners, token_outcomes, token_features. Alembic migration ran on server. |
| 1.2 Solver writes to Postgres | `5f4951c` | `AttemptRecord` + `persist_shadow_attempt_safe` via BackgroundTasks |
| 1.3+1.4 Poller persistence | `54c10a8`, `b31149f`, `6324bfd` | Winner extraction + per-token outcomes with `clearingPrices`/`orders` fallback |
| 1.5 UniV2 router | `59b2b1d` | SushiSwap + Camelot V2, 2-hop search through WETH/USDC/USDT |
| 1.6 Surplus analyzer | `ebdf2c1` | `python -m src.shadow.cli analyze --days N` |
| Big-auction fix | `a8c9c72`, `f2e0042` | Refactored poller to persist ALL auctions (not just <50 orders) |

### Phase 2

| Task | Commit | Note |
|---|---|---|
| 2.1 UniV3 quoter | `a9b5b4b` | QuoterV2 contract via Multicall3, all 4 fee tiers |
| 2.3a Feature extractor | `58305da` (skel) + `1dd606f` (edge) | On-chain features: decimals + pool counts |
| 2.3b/c/d Classifier | `8fe663a` (skel) + `26c1a58` (edge) | LightGBM + RF cold-start, versioned `.pkl` registry, runtime predictor |
| 2.4 Real LP | `2d26c62` (skel) + `2d22852` (edge) | `scipy.optimize.linprog` (HiGHS) with ring balance constraints |
| 2.5 Bipartite matcher | `32f0a22` (skel) + `025f1ce` (edge) | 2-party CoW matcher, greedy by largest sell |
| 2.6 CIP-67 composer | `6987e28` (skel) + `b4fdda9` (edge) | Conservative composer: prices within ±2% merge, conflicts drop lower-surplus |

### Operations / fixes

- GHA `DEPLOY_ENABLED` variable typo fixed (`DEPLOEYED_ENABLED` → `DEPLOY_ENABLED`)
- HETZNER_SSH_KEY discovered to be passphrase-protected → all deploys done manually via my SSH session (user will need to replace with passphrase-less key for automated deploys)
- Server `.env` updated with `SOLVER_DB_PASSWORD`
- Dockerfile updated to bundle `alembic.ini`
- Deploy workflow updated to pull all 3 cow-* containers + run `alembic upgrade head`
- CI fixes: ruff isort `known-first-party=[src,edge,scripts]`, mypy overrides for `edge.*`, `lightgbm/sklearn/pandas/scipy`
- Crontab installed on Hetzner: hourly feature extraction, 6h training, 3h JSONL backup
- Local JSONL backups in `/Users/mma/workspace/cow-solver-skeleton/ops_logs/`

## Current state on server (2026-05-23 00:15 UTC)

```
Containers (all healthy):
  cow-solver        Up 1 min
  cow-shadow-poller Up 33 sec
  cow-reconciler    Up 41 min

Database (solver):
  shadow_auctions:  46
  shadow_solutions: 46
  shadow_winners:   46
  token_outcomes:   360
  token_features:   118
```

## What didn't get done

- **Task 2.2 Pool indexer** (factory WebSocket watcher + Redis cache + scorer) — deferred; requires WS RPC endpoint and is heavyweight to test reliably overnight.
- **Classifier training producing models** — fundamental data issue: the public CoW API does NOT return `clearingPrices` in the `solver_competition` endpoint (verified empty across 20+ samples). Without per-token winner attribution, the `appeared_in_winner` signal is always 0 and the labeler emits nothing useful. This needs either Phase 4 on-chain Settlement-event reconciler OR post-onboarding API access. The pipeline itself is correct and tested — model artifact creation will work as soon as labels arrive.

## What runs automatically overnight

- Shadow poller: polls CoW API every ~60s + backoff, persists every auction to Postgres + JSONL.
- Reconciler: cycles every 60s (currently does little — gets meaningful work in Phase 4).
- Cron `:15`/hour — feature extraction.
- Cron `:30`/6h — classifier training attempt (will skip with `not_enough_samples` until label source improves).
- Cron `:00`/3h — JSONL snapshot to `/var/tmp/cow-backups/`.

## Caveats / what the user should know

1. **Automated deploy is broken.** `HETZNER_SSH_KEY` GHA secret is passphrase-protected. Build pushes succeed to GHCR, but the deploy step always fails on `ssh: this private key is passphrase protected`. Replace the secret with a passphrase-less ed25519 key restricted to this server.

2. **Server is at 90% disk** — survived overnight, but monitor. Docker image churn from frequent rebuilds will eat space. Consider a weekly prune job.

3. **Live solver doesn't /solve much.** Arbitrum auctions routinely have 500-1500 orders; the poller skips per-order fetches when `len(orders) > 50` (rate-limit defense). For those auctions the database gets metadata + winner-side info only — our strategies never run. This is the right tradeoff for Phase 1 data collection but is the next strategic question: do we register as a solver (to get full auctions via push) or accept that we only `/solve` the small batches?

4. **n_orders is now extracted from `raw_competition.auction.orders` correctly** — earlier rows showing `n_orders=0` are pre-fix. New rows will have accurate counts.

5. **The strategy chain order in `load_default_strategies()` matters.** Current order: bipartite → multi-party → long-tail-stub → router-v2 → naive. Composer merges all that succeed. Verify against shadow data once /solve calls accumulate.

## How to inspect overnight progress

```bash
# DB counts
ssh mounir@116.203.81.107 'for t in shadow_auctions shadow_solutions shadow_winners token_outcomes token_features; do printf "%s: " "$t"; docker exec postgres psql -U solver -d solver -tAc "SELECT count(*) FROM $t"; done'

# Surplus + win-rate analysis
ssh mounir@116.203.81.107 'docker exec cow-solver python -m src.shadow.cli analyze --days 1'

# Latest auctions
ssh mounir@116.203.81.107 'docker exec postgres psql -U solver -d solver -c "SELECT auction_id, polled_at, n_orders FROM shadow_auctions ORDER BY polled_at DESC LIMIT 10;"'

# Container health
ssh mounir@116.203.81.107 'docker ps --filter name=cow-'

# Cron log
ssh mounir@116.203.81.107 'tail -20 /opt/mhagentic/stack/cow-solver/data/cron.log'
```
