# cow-solver-skeleton

Public skeleton for a CoW Protocol solver. The competitive edge lives in a
private git submodule under `edge/` and is loaded at runtime if present.

See [design spec](docs/superpowers/specs/2026-05-22-cow-solver-design.md).

## Quick start

```bash
uv sync
uv run pytest
uv run uvicorn --factory src.main:build_default_app --reload
```

## Deployment

See [DEPLOYMENT.md](docs/DEPLOYMENT.md).
