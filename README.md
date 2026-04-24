# jp-adopt-core

Greenfield **JP ADOPT** CRM platform (polyglot monorepo): **FastAPI** + **SQLAlchemy 2** + **Alembic** (`apps/api`), **ARQ** worker for outbox delivery (`apps/worker`), **Next.js** App Router (`apps/web`), and **OpenAPI → TypeScript** (`packages/contracts`). **Postgres** is the system of record; **Redis** backs the ARQ broker.

- **Local development:** [docs/runbooks/spike-local-dev.md](docs/runbooks/spike-local-dev.md)
- **Outbox → webhook:** successful `PATCH /v1/contacts/{id}` writes an `outbox` row in the **same transaction**; the worker POSTs to `INTEGRATION_WEBHOOK_URL` with `X-JP-Signature` = hex HMAC-SHA256 of the request body (see [WEBHOOKS](https://github.com/joshua-project/dt-adoption-platform/blob/main/docs/WEBHOOKS.md) in `dt-adoption-platform`).
- **Auth:** protected routes require a Bearer **Azure AD B2C** JWT when `STRICT_AUTH=true` (set tenant, audience, and optionally issuer; see `.env.example`). For local dev, `STRICT_AUTH=false` allows the documented `dev-local` bypass only when `APP_ENV` / `ENV` is not `production`. In production, the API refuses to start with `STRICT_AUTH=false`, and `Bearer dev-local` returns **403**. Interactive staff sign-in (MSAL + PKCE) is documented in [apps/web/README.md](apps/web/README.md).

## Layout

| Path | Role |
|------|------|
| `apps/api` | REST API, migrations, `openapi.json` export |
| `apps/worker` | ARQ cron: claim outbox rows, sign & POST |
| `apps/web` | Staff UI (spike) |
| `packages/contracts` | `openapi-typescript` output from `apps/api/openapi.json` |
| `docker-compose.yml` | Postgres + Redis |

## Quick start

**First time (or after pulling):** install deps, create `.env` / `apps/web/.env.local` if missing, start Postgres (:5434) + Redis, run migrations:

```bash
pnpm run setup:local
```

**Run API + worker + Next together:**

```bash
pnpm run dev:stack
```

Then open **`/contacts`** on the URL Next prints (often `http://localhost:3000`), use bearer **`dev-local`**, and **Load contacts**. Details: [docs/runbooks/spike-local-dev.md](docs/runbooks/spike-local-dev.md).

## Phase 0 tracking

- Epic **#10**, issues **#11**–**#18** (label `phase-0-spike`).

This repo is separate from the legacy D.T/WordPress integration; this spike does **not** call Disciple.Tools. No N8N is required; any HTTP URL may be used as the integration sink.
