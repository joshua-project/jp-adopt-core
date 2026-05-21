# syntax=docker/dockerfile:1.7
#
# apps/api — FastAPI service container.
#
# Two-stage build:
#   1. builder — uv sync into /opt/venv; copy source.
#   2. runtime — slim image with the venv + source; runs uvicorn.
#
# Used by:
#   - .github/workflows/deploy.yml (production deploys to ACA)
#   - docker-compose.yml (local container-shape verification)
#
# Build context: the repo root, not apps/api/. The compose + workflow
# both set context: . so the worker package is reachable for the
# workspace-dep install. If you `docker build` by hand, do it from the
# repo root: `docker build -f apps/api/Dockerfile -t jp-adopt-api .`

# ─── Builder ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

# Install uv via its official installer pinned by image tag; matches the
# version the GitHub Actions setup-uv@v4 step uses in CI.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1

# Build at /app — the same path the runtime stage uses — so the venv's
# hardcoded shebang lines and console-script paths remain valid after
# the cross-stage COPY. Python venvs are not path-portable without
# this alignment (`exec alembic: no such file or directory` is the
# symptom when builder and runtime paths diverge).
WORKDIR /app

# Workspace pyprojects depend on each other (jp-adopt-worker → jp-adopt-api,
# jp-adopt-etl → jp-adopt-api), so uv invokes the build backend on all of
# them even with --no-install-project. That means the actual source trees
# need to be present before we can sync. One COPY + one sync is correct
# but loses dep-only caching — most iteration uses pnpm dev:stack anyway,
# so the per-Docker-rebuild slowdown is acceptable.
#
# With BuildKit + buildx, add `--mount=type=cache,target=/root/.cache/uv`
# to the RUN below for a ~5x speedup on iterative rebuilds. Omitted by
# default so the Dockerfile works on stock Docker without buildx.
COPY pyproject.toml uv.lock ./
COPY apps/api ./apps/api
COPY apps/worker ./apps/worker
COPY apps/etl ./apps/etl

RUN uv sync --package jp-adopt-api --frozen --no-editable

# Sanity check: confirm the venv landed where we expect. Fails loudly
# at build time if the path alignment regresses.
RUN test -x /app/.venv/bin/alembic && test -x /app/.venv/bin/uvicorn


# ─── Runtime ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# libpq for asyncpg's native bindings (asyncpg statically links most of
# what it needs, but tini + curl land here for PID-1 + healthchecks).
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user — ACA enforces non-root by default in production. The
# UID matches what Azure Container Apps assigns by convention.
RUN groupadd --system --gid 1001 app \
    && useradd --system --gid app --uid 1001 --shell /bin/false app

WORKDIR /app

# Builder used /app as WORKDIR (so venv shebangs are portable). Copy
# the venv + source from those matching paths.
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/apps/api /app/apps/api

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Default port matches the docker-compose ingress + ACA convention.
    PORT=8000

USER app
WORKDIR /app/apps/api

# Run alembic migrations on startup is INTENTIONALLY NOT done here. The
# deploy workflow runs `alembic upgrade head` as a separate CI job
# before rolling new revisions, and local dev uses the seed-local
# script. Doing it in the entrypoint would race multiple replicas.

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "jp_adopt_api.main:app", "--host", "0.0.0.0", "--port", "8000"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1
