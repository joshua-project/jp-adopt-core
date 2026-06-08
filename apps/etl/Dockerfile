# syntax=docker/dockerfile:1.7
#
# apps/etl — DT → jp-adopt-core hourly delta cron.
#
# Mirrors apps/worker/Dockerfile (uv-based two-stage build). Different
# entrypoint (dt-etl is the console script). Build from the repo root:
# `docker build -f apps/etl/Dockerfile -t jp-adopt-etl .`
#
# Deployed as an Azure Container Apps Job (triggerType: Schedule) that
# runs `dt-etl --watermark auto --mode production --table all` hourly.
# See docs/runbooks/dt-cron-sync.md.

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

# Workspace cross-deps mean uv needs the actual source trees present
# before sync, so we copy everything in one shot.
COPY pyproject.toml uv.lock ./
COPY apps/api ./apps/api
COPY apps/worker ./apps/worker
COPY apps/etl ./apps/etl

RUN uv sync --package jp-adopt-etl --frozen --no-editable

# Sanity check: confirm the venv landed where we expect.
RUN test -x /app/.venv/bin/dt-etl


# ─── Runtime ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 1001 app \
    && useradd --system --gid app --uid 1001 --shell /bin/false app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/apps/etl /app/apps/etl

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/New_York

USER app
WORKDIR /app/apps/etl

ENTRYPOINT ["/usr/bin/tini", "--"]
# Default to the hourly delta shape via run-cron.sh, which reads
# DT_MYSQL_URL + DATABASE_URL from the ACA Job's env. A one-off baseline
# or backfill run can override args via `az containerapp job start`.
CMD ["./run-cron.sh"]

# No EXPOSE — the ETL doesn't accept inbound traffic. No HEALTHCHECK
# either; the Container Apps Job replica is short-lived and its exit
# status is the operational signal.
