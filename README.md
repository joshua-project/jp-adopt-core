# jp-adopt-core

Greenfield **JP ADOPT** CRM platform (polyglot monorepo): **FastAPI** + **SQLAlchemy 2** + **Alembic** (`apps/api`), **ARQ** worker for outbox delivery (`apps/worker`), **Next.js** App Router (`apps/web`), and **OpenAPI → TypeScript** (`packages/contracts`). **Postgres** is the system of record; **Redis** backs the ARQ broker.

- **Local development:** [docs/runbooks/spike-local-dev.md](docs/runbooks/spike-local-dev.md)
- **Outbox → webhook:** successful `PATCH /v1/contacts/{id}` writes an `outbox` row in the **same transaction**; the worker POSTs to `INTEGRATION_WEBHOOK_URL` with `X-JP-Signature` = hex HMAC-SHA256 of the request body (see [WEBHOOKS](https://github.com/joshua-project/dt-adoption-platform/blob/main/docs/WEBHOOKS.md) in `dt-adoption-platform`).
- **Auth:** protected routes require a Bearer **Azure AD B2C** JWT when `STRICT_AUTH=true` (set tenant, audience, and optionally issuer; see `.env.example`). For local dev, `STRICT_AUTH=false` allows the documented `dev-local` bypass only.

## Layout

| Path | Role |
|------|------|
| `apps/api` | REST API, migrations, `openapi.json` export |
| `apps/worker` | ARQ cron: claim outbox rows, sign & POST |
| `apps/web` | Staff UI (spike) |
| `packages/contracts` | `openapi-typescript` output from `apps/api/openapi.json` |
| `docker-compose.yml` | Postgres + Redis |

## Quick start

```bash
cp .env.example .env
docker compose up -d
cd apps/api && uv sync && uv run alembic upgrade head
uv run uvicorn jp_adopt_api.main:app --reload --host 0.0.0.0 --port 8000
# other terminals: worker (see runbook) + `pnpm install && pnpm run contracts:generate && pnpm --filter web dev`
```

## Phase 0 tracking

- Epic **#10**, issues **#11**–**#18** (label `phase-0-spike`).

This repo is separate from the legacy D.T/WordPress integration; this spike does **not** call Disciple.Tools. No N8N is required; any HTTP URL may be used as the integration sink.
