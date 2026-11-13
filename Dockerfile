# Multi-stage build aligned with DOCKER_AND_AUTH_STANDARD

# Base stage: runtime foundation + uv binary (use py3.12 to satisfy project requirement)
ARG BASE_REGISTRY=docker.io
FROM ${BASE_REGISTRY}/library/python:3.14-slim AS base
ENV UV_SYSTEM_PYTHON=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/usr/local/bin:$PATH

# Minimal tools for healthcheck and downloads
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Add uv (multi-arch)
COPY --from=ghcr.io/astral-sh/uv:0.10.10 /uv /usr/local/bin/uv

WORKDIR /app

# Builder stage: install deps into .venv
FROM base AS builder

# Build-only deps (kept out of final image)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    libpq-dev \
    git \
 && rm -rf /var/lib/apt/lists/*

# Copy manifests first for caching
COPY README.md pyproject.toml uv.lock ./

# Install dependencies only (cached layer - not invalidated by code changes)
RUN uv sync --frozen --no-dev --no-install-project

# Copy application code after deps
COPY main.py models.py service_auth.py ./
COPY auth ./auth
COPY services ./services
COPY scripts ./scripts
COPY migrations ./migrations
COPY init.sql ./init.sql

# Install the local project (fast - deps already cached)
RUN uv sync --frozen --no-dev

# Production stage: slim runtime
FROM ${BASE_REGISTRY}/library/python:3.14-slim AS production
ENV UV_SYSTEM_PYTHON=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:/usr/local/bin:$PATH \
    PYTHONPATH=/app

WORKDIR /app

# Runtime libs only
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    libpq5 \
 && rm -rf /var/lib/apt/lists/*

# Non-root user created BEFORE the COPYs so `--chown=appuser:appuser` resolves.
RUN useradd -m -u 1000 appuser

# Copy venv and app from builder (ownership set at copy time - no recursive chown)
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/main.py /app/main.py
COPY --from=builder --chown=appuser:appuser /app/models.py /app/models.py
COPY --from=builder --chown=appuser:appuser /app/service_auth.py /app/service_auth.py
COPY --from=builder --chown=appuser:appuser /app/auth /app/auth
COPY --from=builder --chown=appuser:appuser /app/services /app/services
COPY --from=builder --chown=appuser:appuser /app/scripts /app/scripts
COPY --from=builder --chown=appuser:appuser /app/migrations /app/migrations
COPY --from=builder --chown=appuser:appuser /app/init.sql /app/init.sql
COPY --from=builder --chown=appuser:appuser /app/README.md /app/README.md
COPY --from=builder --chown=appuser:appuser /app/pyproject.toml /app/pyproject.toml
COPY --from=builder --chown=appuser:appuser /app/uv.lock /app/uv.lock

# Prepare cert/log dirs for mounted secrets. These are created AFTER the COPYs,
# so they inherit nothing: own them explicitly. /app itself comes from WORKDIR
# (root:root) and needs the same. Non-recursive on purpose - a `chown -R /app`
# rewrites every inode in .venv and produces a fresh, non-dedupable layer on
# every build.
RUN install -d -o appuser -g appuser /app /app/certs /app/logs
USER appuser

EXPOSE 8544

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8544/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8544"]
