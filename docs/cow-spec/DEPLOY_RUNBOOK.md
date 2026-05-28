# cow-stack Deploy Runbook

Step-by-step procedure to bring up the CoW reference autopilot+driver on
Hetzner once PR #44 is merged. Tested order, expected outputs, and the
two most likely failure modes with their remediation.

## Prerequisites

- PR #44 (`feat/cow-stack-integration`) merged to `main`
- GitHub Actions deploy run completed successfully — verify with
  `docker images | grep cow-solver` showing the new SHA tag
- `RPC_ARBITRUM` env var set on Hetzner (already present from existing
  setup — verify with `grep RPC_ARBITRUM /opt/mhagentic/stack/.env`)
- ~1.5 GB free RAM on the Hetzner box — check with `free -m` before starting

## Step 1 — Set rewards address

The skeleton's solver engine now reads `REWARDS_ADDRESS` from env at
startup. For shadow-only runs the placeholder zero-address works; before
Barn onboarding you must replace it with a real address controlled on
both Arbitrum and Ethereum mainnet.

```bash
ssh hetzner
cd /opt/mhagentic/stack
echo "REWARDS_ADDRESS=0x0000000000000000000000000000000000000000" >> .env
# (replace with real address before Barn onboarding)
docker compose restart cow-solver
docker logs cow-solver | grep rewards_address
```

Expected output (truncated): `"rewards_address": "0x000…000"` in the
startup config log.

## Step 2 — Bring up the CoW reference stack

```bash
docker compose --profile cow-stack up -d cow-driver cow-autopilot
```

This starts two new containers alongside the existing five.

Watch the driver come up first:

```bash
docker logs -f cow-driver
```

Expected within ~30s:
- Liquidity indexing log lines: `indexing uniswap-v3 ...`, `indexing
  uniswap-v2 ...`
- A line like `solver mhagentic listening on http://cow-solver:8000`
- Eventually: `driver listening on 0.0.0.0:11088`

If you see panic/exit messages instead, see "Driver fails to start" below.

Then watch the autopilot:

```bash
docker logs -f cow-autopilot
```

Expected within ~12s after the next auction completes upstream:
- `received auction id=...`
- `solving auction id=...`
- `solver mhagentic submitted N solutions`

## Step 3 — Verify solver engine receives requests

```bash
docker logs --since 2m cow-solver | grep -E "POST /solve|POST /notify"
```

Expected: at least one `POST /solve` per Arbitrum auction (~12s interval).
If you see zero `/solve` calls after 2 min, see "Autopilot not dispatching"
below.

## Step 4 — Check for spec validation failures

The most likely first-deploy failure is the driver rejecting our
solutions because of the [interactions schema mismatch flagged in PR
#44 review](https://github.com/mounirhajri/cow-solver-skeleton/pull/44#issuecomment-4561392236).
Use the diagnostic helper:

```bash
python3 scripts/cow_driver_log_diagnose.py --since 5m
```

Sample output:
```
=== Driver decision counts (last 5 min) ===
  solutions_received:           42
  solutions_rejected:           42
  solutions_simulated:           0

=== Top rejection reasons ===
  invalidInteraction (missing 'kind' field): 42
```

If you see 100% rejection with `invalidInteraction`, that's the predicted
schema gap. Capture a sample error and open a follow-up PR with the
exact validation message — we then patch `encoder/interactions.py` to
emit `kind/inputs/outputs` per the driver's actual requirements.

## Step 5 — Let it run for 24 h

If Step 4 shows non-zero simulated solutions, the driver IS giving us
real Tier-3 simulation data. Let it run for 24 h to accumulate a
significant sample.

After 24 h, run:

```bash
python3 scripts/cow_driver_log_diagnose.py --since 24h --full
```

This prints simulation outcomes per strategy and per auction — the gold
standard data we've been missing to close the Tier-3 verification gap.

## Failure modes and remediation

### Driver fails to start

Most common cause: malformed `driver.shadow.toml` — typo in a token
address or missing required field.

```bash
docker logs cow-driver | head -50
```

Look for `Error: invalid configuration ...`. Fix the field, then:

```bash
docker compose restart cow-driver
```

### Autopilot not dispatching

The autopilot polls the CoW orderbook every few seconds for new auctions.
Common causes:

1. **`--shadow` URL unreachable** — verify with
   `curl https://api.cow.fi/arbitrum_one/api/v1/version` from the host.
2. **No active auctions in the window** — Arbitrum sees ~36 auctions per
   hour. If you started during a quiet patch, wait 5 min.
3. **Driver health check failing** — autopilot won't dispatch to drivers
   that don't respond to its health check. Verify
   `curl http://localhost:11088/health` returns 200.

### High driver RAM usage / OOM kills

The CoW reference driver indexes pool state on every Arbitrum block. On
a CX22 (4 GB) with other services already running, this is tight.
Symptoms: `dmesg` shows `oom-killer` invocations, containers restart.

Mitigation in priority order:
1. Stop one of the less critical existing containers temporarily (e.g.
   `cow-reconciler`)
2. Disable a liquidity source in `driver.shadow.toml` to reduce indexing
   memory (`uniswap-v3` is the biggest)
3. Upgrade to CX32 (~€8/month) — recommended if running >24 h

### RPC rate limiting

The driver consumes 5-10 RPC requests per second during indexing,
dropping to 1-2/s steady-state. Public Arbitrum RPCs will rate-limit
within minutes.

Symptom: driver logs `rpc error: too many requests`.

Fix: switch `RPC_ARBITRUM` to an Alchemy/QuickNode paid endpoint and
restart cow-driver.

## Tear-down

```bash
docker compose --profile cow-stack down
```

Stops cow-driver + cow-autopilot. The existing five services keep
running.

## Next steps after a clean 24 h run

1. Open follow-up PR with whatever interaction-schema fix the driver
   logs revealed needed
2. Pipe `cow-driver` simulation outcomes into `shadow_solutions` so
   they're queryable alongside our existing shadow scores
3. Schedule the Barn onboarding call — at this point we have empirical
   on-simulation data that's much more credible than naked shadow win-rate
