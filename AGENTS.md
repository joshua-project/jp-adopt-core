# jp-adopt-core — agent orientation

A Next.js + FastAPI + Postgres + ARQ-worker monorepo that runs Joshua
Project's adoption program. Greenfield replacement for the
Disciple.Tools-based system. Staff-facing UI is in `apps/web`; the
public intake forms live in the separate `jp-adopt-forms` repo and
POST to this API.

Read this whole file before making changes. The conventions are
load-bearing; ignoring them silently is the most common way work in
this repo breaks.

## Workspace layout

```
apps/
  api/        FastAPI 0.1, asyncpg + SQLAlchemy 2.0 async, Alembic
  worker/     ARQ (Redis) — outbox drain, drip send, daily digest
  web/        Next.js 15 App Router + Tailwind + MSAL.js
  etl/        Sync (psycopg2 + pymysql) DT → Postgres importer
packages/
  contracts/  Generated TS types from apps/api/openapi.json
docs/
  runbooks/   Operator-facing how-tos (one per system)
  solutions/  Documented learnings from past problems, organized by
              category with YAML frontmatter (module, tags,
              problem_type). Relevant when implementing or debugging
              in documented areas — conventions/, database-issues/,
              etc.
  follow-ups/ Parked items from prior PR reviews
scripts/
  setup-local.sh    one-time bootstrap (.env, deps, migrations)
  dev-stack.sh      `pnpm run dev:stack` runner (Postgres + Redis + API + worker + web)
  seed-local.sh     idempotent test data (dev-local staff, 2 test
                    facilitators, drip campaign, 3 test adopters)
  smoke-local.sh    12-check end-to-end smoke against a running stack
```

Top-level `package.json` scripts: `setup:local`, `dev:stack`,
`openapi:export`, `contracts:generate`, `lint:web`.

## Local dev (the fast path)

```bash
pnpm run setup:local                        # one time
pnpm run dev:stack                          # foreground; Ctrl-C stops everything
scripts/seed-local.sh                       # populate test data
scripts/smoke-local.sh                      # verify 12 checkpoints
```

API on `:8000`, web on `:3000` (native) or `:3030` (docker compose),
Postgres on host `:5434`, Redis on `:6379`. Auth in dev: paste
`Bearer dev-local` in the dev-token textbox; `STRICT_AUTH=false` makes
the API accept it. See `docs/runbooks/local-dev.md` for the docker
compose + Tailscale paths.

**Tests:**

```bash
cd apps/api && uv run --extra dev pytest        # API tests (Postgres required)
pnpm --filter web test                          # Web tests (vitest + RTL)
pnpm --filter web test:watch                    # Web tests in watch mode
```

Vitest config lives at `apps/web/vitest.config.ts`; global setup
(jest-dom matchers, cleanup) at `apps/web/src/test/setup.ts`. Tests
go in `__tests__/` folders next to the code, or as `*.test.ts(x)`
siblings. CI runs both suites on every PR.

## Conventions specific to this codebase

- **State-machine via HTTP, not generic PATCH.** `adopter_status` and
  `facilitator_status` were intentionally removed from `ContactPatch`
  (`apps/api/src/jp_adopt_api/schemas.py`). The only valid path to
  mutate them is `POST /v1/contacts/{id}/transition` (workflow router)
  or `POST /v1/matches/{id}/decide`. Don't add them back to PATCH.
- **`Bearer dev-local`** must never reach production. The boot-time
  validator in `apps/api/src/jp_adopt_api/config.py` refuses to start
  when `APP_ENV=production` AND `STRICT_AUTH=false`. Don't loosen.
- **Transactional outbox pattern.** Every state change writes to the
  `outbox` table in the same transaction; the worker drains it. Use
  `emit_outbox()` from `outbox_suppression.py`; never call a webhook
  client directly from a handler. Bulk operations (ETL) use
  `outbox_suppressed()` to skip the writes entirely.
- **Optimistic locking on Contact.** `Contact.version` gates writes;
  `SELECT FOR UPDATE` + version check is the canonical update shape.
- **Partial unique indexes for ON CONFLICT.** Several upserts target
  partial unique indexes (e.g. `uq_contacts_source_system_source_id`
  filtered by NOT NULL); the ON CONFLICT clause needs the same
  `index_where=` predicate or it raises.
- **Never edit an applied Alembic migration.** Create a new
  revision instead. See `docs/solutions/conventions/alembic-migration-edit-after-apply-2026-05-20.md`
  for the failure mode.
- **Enum values get explicit per-kind label tables.** Don't render
  `do_not_engage` directly or via a mechanical underscore-to-space
  helper. Use `humanizeStatus(value, kind)` /
  `humanizeReasonCode(value)` from `apps/web/src/lib/vocab.ts`. See
  `docs/solutions/conventions/enum-to-ui-label-vocab-2026-05-21.md`.
- **OpenAPI is the source of truth for the web client.** After any
  API surface change, run `pnpm contracts:generate` or CI will fail
  on the "contracts artifact must be committed" check.

## Don't touch without explicit reason

- `auth.py` issuer-regex dispatch (multi-IdP routing).
- The `_DEV_BEARER_TOKEN` literal — it's shared between `auth.py` and
  `deps.py` deliberately so renaming touches one constant.
- Migration files that have been merged. New change → new revision.

## When you're stuck or about to do something new

Check `docs/solutions/` first — past learnings are organized by
category (`conventions/`, `database-issues/`, etc.) with searchable
YAML frontmatter (`module`, `tags`, `problem_type`). Also
`docs/runbooks/` for operator-facing how-tos: `local-dev.md`,
`drip-engine.md`, `daily-digest.md`, `deploy.md`, `secret-rotation.md`,
`matching-algorithm-v1.md`, `dt-cutover.md`, `operator-handbook.md`,
`quick-start.md`, `user-testing-walkthrough.md`, `amy-walkthrough.md`,
`magic-link-side-car.md`, `multi-idp-b2c.md`.

If you solve a new non-trivial problem, run `/ce-compound` to capture
it as a `docs/solutions/` entry so the next agent finds it.
