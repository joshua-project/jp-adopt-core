#!/usr/bin/env bash
# One-time / repeat local bootstrap: env files, deps, Docker DB/Redis, migrations.
# Does not start API/worker/web — use `pnpm run dev:stack` after this.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "error: missing command '$1' — install it and retry." >&2
    exit 1
  fi
}

need_cmd docker
need_cmd uv
need_cmd pnpm

if ! docker info >/dev/null 2>&1; then
  echo "error: Docker is not running (docker info failed). Start Docker Desktop and retry." >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example (Path A: STRICT_AUTH=false, dev-local bearer)."
fi

if [[ ! -f apps/web/.env.local ]]; then
  cp apps/web/.env.local.example apps/web/.env.local
  echo "Created apps/web/.env.local from .env.local.example (dev contacts UI; no B2C until you set NEXT_PUBLIC_*)."
fi

pnpm install

echo "Starting Postgres (host :5434) and Redis (:6379), waiting for health…"
docker compose up -d --wait --wait-timeout 120 postgres redis

set -a
# shellcheck disable=SC1091
source .env
set +a

echo "Installing Python deps and running migrations…"
(cd apps/api && uv sync --extra dev && uv run alembic upgrade head)
(cd apps/worker && uv sync)

echo ""
echo "Local bootstrap complete."
echo "  Next:  pnpm run dev:stack"
echo "  Then:  http://localhost:3000/contacts (or the port Next prints) — bearer dev-local → Load contacts"
echo "  API:   http://127.0.0.1:8000/docs"
