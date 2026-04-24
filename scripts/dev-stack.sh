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

docker compose up -d --wait --wait-timeout 120 postgres redis

set -a
# shellcheck disable=SC1091
source .env
set +a

API_PORT="${API_PORT:-8000}"

(cd apps/api && uv sync --extra dev && uv run alembic upgrade head)
(cd apps/worker && uv sync)

cleanup() {
  local p
  for p in "${PIDS[@]:-}"; do
    kill "$p" 2>/dev/null || true
  done
}
PIDS=()
trap cleanup EXIT INT TERM

(cd apps/api && uv run uvicorn jp_adopt_api.main:app --reload --host 0.0.0.0 --port "${API_PORT}") &
PIDS+=($!)
(cd apps/worker && uv run jp-adopt-worker) &
PIDS+=($!)
(cd "$ROOT" && pnpm --filter web dev) &
PIDS+=($!)

echo "jp-adopt dev stack: API http://127.0.0.1:${API_PORT}  (set API_PORT to change; match NEXT_PUBLIC_API_URL in apps/web/.env.local)" >&2

wait
