# cow-solver-skeleton

Public skeleton for a CoW Protocol solver competing on Arbitrum. The
competitive edge (CoW-matching + long-tail pool indexer) lives in a private
git submodule under `edge/` and is loaded at runtime if present.

See [design spec](docs/superpowers/specs/2026-05-22-cow-solver-design.md).

## Local development

```bash
uv sync
uv run pytest
uv run uvicorn --factory src.main:build_default_app --reload
```

## Local shadow test

```bash
# 1inch API key required
echo "ONEINCH_API_KEY=your-key" > .env
docker compose --profile shadow up -d
docker compose logs -f cow-solver
```

The shadow driver receives real Arbitrum batches and calls `cow-solver:8000/solve`.

## Deployment

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
