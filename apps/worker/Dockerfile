# syntax=docker/dockerfile:1.7
#
# apps/worker — ARQ outbox dispatcher + drip drain + digest cron.
#
# Mirrors apps/api/Dockerfile (uv-based two-stage build). Different
# entrypoint (jp-adopt-worker) and no exposed port. Build from the
# repo root: `docker build -f apps/worker/Dockerfile -t jp-adopt-worker .`

# ─── Builder ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1

# Build at /app to match the runtime WORKDIR — venv shebangs are not
# path-portable across stages. See apps/api/Dockerfile for context.
WORKDIR /app

# See apps/api/Dockerfile for the rationale: workspace cross-deps mean
# uv needs the actual source trees present before sync, so we copy
# everything in one shot.
#
# Add `--mount=type=cache,target=/root/.cache/uv` to the RUN below when
# building with BuildKit + buildx for ~5x faster rebuilds.
COPY pyproject.toml uv.lock ./
COPY apps/api ./apps/api
COPY apps/worker ./apps/worker
COPY apps/etl ./apps/etl

RUN uv sync --package jp-adopt-worker --frozen --no-editable

# Sanity check: confirm the venv landed where we expect.
RUN test -x /app/.venv/bin/jp-adopt-worker


# ─── Runtime ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 1001 app \
    && useradd --system --gid app --uid 1001 --shell /bin/false app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/apps/worker /app/apps/worker

# Worker reads MJML templates for the drip + digest render paths. Same
# files as the API copies; this duplicate copy keeps the container
# self-contained without a shared volume.
COPY --from=builder --chown=app:app /app/apps/api/email-templates /app/apps/api/email-templates

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # ARQ defaults to America/New_York via this env var so the daily-
    # digest internal hour gate doesn't have to coordinate with the
    # container's clock. Override at deploy time if needed.
    TZ=America/New_York

USER app
WORKDIR /app/apps/worker

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["jp-adopt-worker"]

# No EXPOSE — the worker doesn't accept inbound traffic. No HEALTHCHECK
# either (ARQ doesn't have a built-in health endpoint; ACA's revision
# liveness is the operational signal).
