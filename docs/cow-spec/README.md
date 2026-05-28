# CoW Protocol Stack Integration

This directory holds the CoW driver/autopilot OpenAPI specs and integration
notes. The specs are the authoritative contract our `/solve` endpoint must
match. Bump them by re-downloading from `cowprotocol/services`:

```bash
curl -s https://raw.githubusercontent.com/cowprotocol/services/main/crates/solvers/openapi.yml \
  -o docs/cow-spec/solver-engine-openapi.yml
curl -s https://raw.githubusercontent.com/cowprotocol/services/main/crates/driver/openapi.yml \
  -o docs/cow-spec/driver-openapi.yml
curl -s https://raw.githubusercontent.com/cowprotocol/services/main/crates/driver/example.toml \
  -o docs/cow-spec/driver-example.toml
```

## Running the local CoW reference stack

The repository ships a `cow-stack` docker compose profile that starts the
reference autopilot + driver alongside our solver engine. Auctions flow
end-to-end: CoW production orderbook → autopilot (shadow mode) → driver →
our `/solve` endpoint → driver simulation → back to autopilot (no on-chain
submission).

### Prerequisites

- `RPC_ARBITRUM` env var pointing to an Arbitrum One archive node
  (Alchemy paid tier recommended; the reference stack is RPC-heavy)
- ~1.5 GB free RAM beyond the existing services
- `config/driver.shadow.toml` populated (Arbitrum addresses + AMM presets)

### Start the stack

```bash
docker compose --profile cow-stack up -d cow-driver cow-autopilot
docker logs -f cow-autopilot   # watch for "new auction received"
docker logs -f cow-driver      # watch for /solve dispatches
docker logs -f cow-solver      # confirm our engine receives requests
```

### Stop the stack

```bash
docker compose --profile cow-stack down
```

The base services (cow-solver, cow-shadow-poller, cow-reconciler,
cow-ghost-refresh, cow-solver-comp-sync) keep running. Stopping cow-stack
only tears down autopilot + driver.

### Operational notes

- **RPC consumption**: the reference driver indexes pool state on every
  block and runs ABI calls for liquidity discovery. A public RPC endpoint
  will be rate-limited within minutes. Provision Alchemy or QuickNode
  before running >30 min sessions.
- **Memory pressure**: each Rust process uses 500 MB – 1 GB. Watch
  `docker stats` on the host. On Hetzner CX22 (4 GB total) the stack is
  tight; upgrade to CX32 if OOM kills start.
- **Submission keys**: the driver config carries a placeholder solver
  account. CoW issues real submission keys during the Barn onboarding
  call; do not put a real key here until that point.
- **No on-chain effects**: the `--shadow` flag on autopilot prevents
  settlement broadcasting. Even though the config points at the production
  orderbook, no tx ever reaches the chain.

### Tier-3 verification path

The driver's post-process step simulates every solution we return. The
result (settles / reverts / partial fill / gas cost) is logged. Parsing
those logs against our `shadow_solutions` rows quantifies how much of our
shadow win-rate would actually settle on-chain — the verification gap
we've been carrying as Tier 3.

A future helper script will aggregate driver simulation outcomes per
auction and write them into the database for systematic comparison.
