# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5.4 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-install-project --no-dev || uv sync --no-install-project --no-dev

COPY src ./src
COPY scripts ./scripts
COPY edge ./edge
COPY alembic.ini ./
RUN uv sync --frozen --no-dev || uv sync --no-dev


FROM python:3.12-slim AS runtime

RUN useradd -m -u 1000 app && \
    mkdir -p /data && chown app:app /data

WORKDIR /app
COPY --from=builder --chown=app:app /app /app

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health').read()" || exit 1

CMD ["uvicorn", "--factory", "src.main:build_default_app", "--host", "0.0.0.0", "--port", "8000"]
