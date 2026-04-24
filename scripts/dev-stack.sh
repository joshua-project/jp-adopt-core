#!/usr/bin/env bash
# One-shot local stack: Postgres/Redis, migrations, API, worker, and web (foreground via wait).
# Prerequisites: Docker, uv, pnpm; run `pnpm install` once at repo root. Copy `.env.example` → `.env`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "error: .env not found — copy .env.example to .env and adjust values." >&2
  exit 1
fi

docker compose up -d postgres redis

set -a
# shellcheck disable=SC1091
source .env
set +a

(cd apps/api && uv sync && uv run alembic upgrade head)
(cd apps/worker && uv sync)

cleanup() {
  local p
  for p in "${PIDS[@]:-}"; do
    kill "$p" 2>/dev/null || true
  done
}
PIDS=()
trap cleanup EXIT INT TERM

(cd apps/api && uv run uvicorn jp_adopt_api.main:app --reload --host 0.0.0.0 --port 8000) &
PIDS+=($!)
(cd apps/worker && uv run jp-adopt-worker) &
PIDS+=($!)
(cd "$ROOT" && pnpm --filter web dev) &
PIDS+=($!)

wait
