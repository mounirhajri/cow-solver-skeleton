# Deployment

## Target environment

- Hetzner CX22 at the shared mhagentic stack (`/opt/mhagentic/stack/`).
- AI Backoffice MUST be stopped before solver runs in shadow phase
  (RAM contention — see [design spec §4.1](superpowers/specs/2026-05-22-cow-solver-design.md)).

## Initial setup (one-time)

1. SSH to Hetzner.
2. Stop Backoffice:
   ```bash
   cd /opt/mhagentic/stack
   docker compose stop backoffice-api backoffice-worker backoffice-api-staging backoffice-worker-staging ollama whisper grafana prometheus
   ```
3. Create solver dir and copy compose snippet:
   ```bash
   mkdir -p /opt/mhagentic/stack/cow-solver/config /opt/mhagentic/stack/cow-solver/data
   ```
4. Append `cow-solver/docker-compose.yml` to top-level `stack/docker-compose.yml` include section.
5. Set env vars in `cow-solver/.env`:
   ```
   ONEINCH_API_KEY=...
   RPC_ARBITRUM=https://arb1.arbitrum.io/rpc
   SOLVER_TAG=latest
   ```
6. Trigger first deploy: `git push origin main` (will use latest GHCR image).

## Monitoring

- `http://<server>:8001/metrics` — Prometheus scrape target
- Logs: `docker compose logs -f cow-solver`
- Shadow data: `/opt/mhagentic/stack/cow-solver/data/shadow.jsonl`

## Rollback

```bash
docker compose pull cow-solver
SOLVER_TAG=<previous-sha> docker compose up -d cow-solver
```
