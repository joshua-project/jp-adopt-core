# Phase 0 spike — local development

This runbook covers the polyglot stack: **Postgres** (system of record), **Redis** (ARQ broker), **FastAPI** (`apps/api`), **ARQ worker** (`apps/worker`), and **Next.js** (`apps/web`).

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (Python 3.12+)
- [pnpm](https://pnpm.io/) 9.x
- Docker (for Postgres + Redis)

## Local API auth: two paths

Pick **one** path for `GET /v1/contacts` and other `/v1/*` routes. The API reads `APP_ENV` or `ENV` (same meaning) plus `STRICT_AUTH` from the repo root `.env` (or your shell).

### Path A — No Azure AD B2C (fastest onboarding)

Use this when you do not have tenant credentials or only need the outbox / webhook spike.

1. Copy `.env.example` → `.env`.
2. Set **`APP_ENV=development`** (or omit it; default is `development`).
3. Set **`STRICT_AUTH=false`**.
4. Call the API with **`Authorization: Bearer dev-local`** (the Next.js Contacts page can use the same token).

Do **not** use Path A in production. With **`APP_ENV=production`** or **`ENV=production`** (or `prod`), the API **refuses to start** if `STRICT_AUTH=false`, and **`Bearer dev-local`** is rejected with **403** even if configuration were misapplied.

### Path B — Real Azure AD B2C (token-acquired API testing)

Use this to validate JWT verification against your tenant (see **Azure AD B2C (API JWTs)** below).

1. Copy `.env.example` → `.env` and set all **`AZURE_AD_B2C_*`** variables for your app registration(s), policy, and API **audience** (see table below).
2. Set **`STRICT_AUTH=true`** so the API only accepts real JWTs (recommended; matches staging/production behavior).
3. **Obtain an access token** whose **`aud`** matches `AZURE_AD_B2C_AUDIENCE` and **`iss`** matches your policy (see optional `AZURE_AD_B2C_ISSUER`):
   - From a registered SPA or Postman using the authorization-code flow with PKCE against your B2C policy, or
   - From browser **DevTools → Network** after signing in with a test app that requests your API scope, by copying the **access token** (not the ID token) from the token response.
4. Paste the token into the Contacts page (or send it as `Authorization: Bearer <jwt>`). Interactive Next.js sign-in is tracked separately (Phase C / MSAL).

### One command: infra, migrations, API, worker, web

From the repo root (after `pnpm install` and `.env` present):

```bash
pnpm run dev:stack
```

This runs Docker Compose for Postgres and Redis, applies Alembic migrations, then starts the API, worker, and web in parallel. Stop with **Ctrl+C** (child processes are torn down).

## 1. Infra: Postgres and Redis

From the repo root:

```bash
docker compose up -d postgres redis
```

Defaults (see `docker-compose.yml`):

| Service  | Port   | User / DB   | Password  |
|----------|--------|-------------|-----------|
| Postgres | 5434 (host → 5432 in container) | `jp_adopt`  | `jp_adopt`|
| Redis    | 6379   | —           | —         |

Copy `.env.example` to `.env` at the repo root (or export the same variables in your shell). The API and worker read `DATABASE_URL`, `REDIS_URL`, and webhook settings from the environment.

## 2. Database migrations

```bash
cd apps/api
uv sync
set -a && source ../../.env && set +a   # or: export $(grep -v '^#' ../../.env | xargs)
uv run alembic upgrade head
```

`DATABASE_URL` must use the **async** driver, e.g. `postgresql+asyncpg://…` (see `.env.example`).

## 3. API (FastAPI)

In one terminal, from `apps/api`:

```bash
uv run uvicorn jp_adopt_api.main:app --reload --host 0.0.0.0 --port 8000
```

- OpenAPI: `http://127.0.0.1:8000/openapi.json`
- Interactive docs: `http://127.0.0.1:8000/docs`
- A seed contact is created by the initial migration (`aaaaaaaa-…`); use `PATCH /v1/contacts/{id}` to trigger the **transactional outbox** (outbox row + contact update in one commit).

## 4. Worker (ARQ + outbox delivery)

The worker uses **Redis** as the ARQ broker. It runs a cron job that claims unprocessed `outbox` rows and POSTs them to `INTEGRATION_WEBHOOK_URL` with an **HMAC-SHA256** signature on the **exact JSON body** bytes:

- Header: `X-JP-Signature: <hex digest of HMAC-SHA256(WEBHOOK_HMAC_SECRET, body)>`
- The body is **canonical JSON** (sorted object keys, no extra whitespace), so verifiers can recompute the digest from the raw request body. This matches the pattern described in the adoption platform’s `docs/WEBHOOKS.md` (hex HMAC of the request body; compare with a timing-safe equality check).

From the repo root (with `.env` loaded):

```bash
cd apps/worker
uv sync
uv run jp-adopt-worker
```

For a quick end-to-end check, set `INTEGRATION_WEBHOOK_URL` to a request inspector (e.g. [webhook.site](https://webhook.site)) and the same `WEBHOOK_HMAC_SECRET` in that tool’s test verifier if you have one, or point at a local stub that logs headers and body.

## 5. Regenerate `openapi.json` and TypeScript types

The OpenAPI spec is written to `apps/api/openapi.json`, then `packages/contracts` generates `src/generated/api.ts` for the web app and other TS clients.

From the **repository root**:

```bash
cd apps/api && uv run python -m jp_adopt_api.scripts.export_openapi
cd ../..
pnpm install
pnpm run contracts:generate
```

## 6. Web (Next.js)

```bash
pnpm install
cp apps/web/.env.local.example apps/web/.env.local
pnpm --filter web dev
```

App: `http://localhost:3000` — the **Contacts** page calls `GET /v1/contacts` with `Authorization: Bearer …`.

## Azure AD B2C (API JWTs)

The API validates **access tokens** (Bearer JWT) for routes under `/v1/*`. Configure (see also `.env.example`):

| Variable | Purpose |
|----------|---------|
| `AZURE_AD_B2C_TENANT_NAME` | B2C tenant name (subdomain, e.g. `contoso` from `contoso.b2clogin.com`) |
| `AZURE_AD_B2C_TENANT_ID` | Directory (tenant) ID GUID |
| `AZURE_AD_B2C_CLIENT_ID` | App registration used by clients (e.g. Next) |
| `AZURE_AD_B2C_POLICY` | User flow / policy name |
| `AZURE_AD_B2C_AUDIENCE` | **aud** to validate (often the API’s Application ID URI or app ID) |
| `AZURE_AD_B2C_JWKS_URI` | (Optional) override JWKS URL; default is derived from tenant + policy |
| `AZURE_AD_B2C_ISSUER` | (Optional) override expected **iss**; default `https://{TENANT_NAME}.b2clogin.com/{TENANT_ID}/v2.0/` — some policies require a more specific issuer; set explicitly if validation fails. |
| `APP_ENV` / `ENV` | `development` (default) for local Path A. Use `production` / `prod` only with **`STRICT_AUTH=true`** — otherwise the process exits at startup. |
| `STRICT_AUTH` | `true` for real JWTs only. `false` only for non-production Path A (`dev-local`). |

The Next app does not embed a full B2C sign-in in this spike; use Path A (`dev-local`) or Path B (paste an access token), as described in **Local API auth: two paths** above.

## Minimal smoke test

1. `docker compose up -d`
2. `alembic upgrade head`
3. Start API, worker, and web
4. `GET /v1/contacts` with `Bearer dev-local` (if `STRICT_AUTH=false`)
5. `PATCH /v1/contacts/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa` with JSON body, e.g. `{"display_name": "Test"}`, same bearer
6. Confirm a new `outbox` row and, after the worker tick, a POST to your webhook URL with `X-JP-Signature` set

```sql
SELECT id, event_type, processed_at FROM outbox ORDER BY created_at DESC LIMIT 5;
```

## Troubleshooting

**“Load contacts” fails in the browser (network error / empty list) but `curl` to the API works:** the browser enforces **CORS**. In **non-production**, the API allows `http://` and `https://` for **`localhost`** and **`127.0.0.1`** on **any port** (so Next on `3001`, etc.). Restart the API after upgrading. For **production**, set comma-separated **`CORS_ALLOW_ORIGINS`** in `.env` to your real web origins.

## Related GitHub work

- Epic: **#20** (blocker closure), **#10** (Phase 0 umbrella)
- Phase A (local dev hardening): **#21**
- Phase 0 spike: **#11**–**#18** (label `phase-0-spike`)
