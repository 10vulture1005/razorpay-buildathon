# syntax=docker/dockerfile:1.6
# Multi-target image:
#   docker build --target api     -t recovery-api    .
#   docker build --target worker  -t recovery-worker .
#   docker build --target migrate -t recovery-migrate .
# `docker build .` (no target) defaults to `api`.

# ---- builder stage (cached deps layer) ----
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv/app

# Build psycopg2-binary wheels ahead of time so the runtime image stays slim.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt


# ---- runtime stage ----
FROM python:3.12-slim AS runtime-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    # tini reaps zombies + forwards signals; required for graceful shutdown
    # of multi-worker uvicorn.
    TINI_VERSION=v0.19.0

# Runtime-only system deps. libpq5 is needed at runtime; gcc/build-essential
# are NOT (they were only in the builder stage).
RUN apt-get update && apt-get install -y --no-install-recommends \
    tini libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps from the builder stage.
COPY --from=builder /install /usr/local

WORKDIR /srv/app

# App code (copied as a single layer for cache friendliness; tests/CI use
# bind mounts instead of the baked image).
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY app ./app
COPY app/policy/policy_config.yaml /srv/app/app/policy/policy_config.yaml

# Non-root runtime user (uid 10001) so a container escape cannot escalate to
# root on the host.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /srv/app
USER appuser

EXPOSE 8000

# Default worker config: 2 workers, single thread each. Override with
# WEB_CONCURRENCY at runtime. 2 is enough for a free-tier Render plan and
# keeps the in-process rate limiter / LLM client scope local to the worker.
ENV WEB_CONCURRENCY=2 \
    UVICORN_HOST=0.0.0.0 \
    UVICORN_PORT=8000 \
    UVICORN_TIMEOUT_KEEP_ALIVE=30 \
    UVICORN_LOG_LEVEL=info

HEALTHCHECK --interval=30s --timeout=5s --start_period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/readyz')" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]


# ---- API target (default) ----
FROM runtime-base AS api

# `tini` is PID 1 and forwards SIGTERM to uvicorn so workers shut down cleanly
# when the orchestrator asks (Render / Fly / k8s).
CMD ["sh", "-c", "exec uvicorn app.main:app \
    --host ${UVICORN_HOST} --port ${UVICORN_PORT} \
    --workers ${WEB_CONCURRENCY} \
    --timeout-keep-alive ${UVICORN_TIMEOUT_KEEP_ALIVE} \
    --log-level ${UVICORN_LOG_LEVEL} \
    --proxy-headers \
    --forwarded-allow-ips='*'"]


# ---- Worker target ----
FROM runtime-base AS worker

CMD ["python", "-m", "app.workers.outcome_poller", "30"]


# ---- Migration target (one-shot) ----
FROM runtime-base AS migrate

CMD ["python", "-m", "scripts.migrate", "upgrade", "head"]


# Bare default alias for `docker build .` -> api.
FROM api AS default
