# Local development

Two supported paths. Pick by what you're optimizing for.

> **Note on `docker compose` vs `docker-compose`:** This runbook uses the v2
> plugin form (`docker compose`, with a space). If your environment only has
> the legacy standalone binary (`docker-compose`, with a hyphen), every command
> here works identically — just substitute. The `docker-compose.yml` file is
> compose-spec compliant and supported by both.

| Path | Best for | Iteration speed |
|------|----------|------------------|
| `pnpm run dev:stack` | Active development (Python / TypeScript hot reload, fast restart) | Sub-second on file save |
| `docker compose --profile full up` | Verifying container-shape behavior, onboarding a new contributor without local Python/Node toolchains, debugging deploy-only issues | ~30s rebuild per code change (or use `--build` only when needed) |

Both run against the **same** Postgres + Redis (via the `postgres` + `redis` services on host ports 5434 / 6379), so you can mix them — bring up infra in Docker, run the API natively, then `docker compose run migrate` if you need to bump migrations from a clean DB.

---

## Path 1: Native dev stack (recommended for day-to-day)

One-time setup (creates `.env` from template, starts Postgres + Redis, syncs Python + Node deps, runs migrations):

```bash
pnpm run setup:local
```

Then every session (foreground; Ctrl-C stops everything):

```bash
pnpm run dev:stack
```

This runs:
- Postgres (Docker) on `127.0.0.1:5434`
- Redis (Docker) on `127.0.0.1:6379`
- API (uvicorn `--reload`) on `127.0.0.1:8000`
- Worker (ARQ) against the local Redis
- Web (Next.js dev) on `127.0.0.1:3000`

Endpoints:
- API: http://127.0.0.1:8000/healthz
- API docs: http://127.0.0.1:8000/docs
- Web: http://localhost:3000

---

## Path 2: Full container stack

Build + bring up the whole stack in containers. Useful for verifying the production image shape, onboarding, or reproducing a CI-only issue.

```bash
# First time (or whenever Dockerfiles change). Builds all three images
# + brings up postgres + redis + runs the one-shot migrate service + starts
# api + worker + web.
docker compose --profile full up --build

# Subsequent runs (no rebuild needed):
docker compose --profile full up
```

The `--profile full` flag opts in to the application containers (api, worker, web). Without it, you get just the infra containers (postgres + redis), which is what `pnpm run dev:stack` consumes.

The `migrate` service runs `alembic upgrade head` exactly once at startup. The api + worker services depend on it via `service_completed_successfully`, so they wait for migrations before starting.

### Running just the infra (no app containers)

```bash
docker compose up postgres redis
```

This is what `pnpm run setup:local` and `pnpm run dev:stack` invoke under the hood. You can do the same by hand and then run the app processes natively.

### Running just the one-shot migrate

```bash
docker compose --profile migrate run --rm migrate
```

Useful when iterating on a migration: bring up postgres, run migrate, run pytest, drop the DB, repeat.

### Stopping + cleaning up

```bash
# Stop containers but keep data
docker compose --profile full down

# Stop + drop the Postgres volume (full reset)
docker compose --profile full down -v
```

---

## Accessing the stack from another device (Tailscale / LAN)

By default the web container's API URL is baked in as `http://127.0.0.1:8000`, which only works from the host machine's browser. To open the stack from your phone, tablet, or another laptop on the same Tailscale tailnet (or LAN), rebuild the web image with a hostname remote peers can reach:

```bash
# Tailscale (recommended — MagicDNS survives IP changes)
export NEXT_PUBLIC_API_URL="http://$(hostname -s).<your-tailnet-name>.ts.net:8000"

# Or by Tailscale IP
export NEXT_PUBLIC_API_URL="http://$(tailscale ip -4):8000"

# Or by LAN IP (less reliable across reboots; fine for short sessions)
export NEXT_PUBLIC_API_URL="http://$(ipconfig getifaddr en0):8000"

docker compose --profile full build web
docker compose --profile full up -d
```

Then open `http://<that-host>:3000` from any peer device.

The API's dev-mode CORS regex already accepts:
- `localhost` / `127.0.0.1`
- `100.x.x.x` (entire Tailscale CGNAT range)
- `10.x.x.x` / `172.16-31.x.x` / `192.168.x.x` (RFC 1918 private networks)
- `*.ts.net` (any Tailscale MagicDNS hostname)

…so no API rebuild is needed. Production CORS still uses the explicit `CORS_ALLOW_ORIGINS` allow-list; the widened regex never runs in production.

**Trust model note:** the dev CORS regex trusts the entire Tailscale CGNAT range and RFC 1918. Network reachability is the security boundary here, not CORS — the dev API runs with `STRICT_AUTH=false` and accepts `Bearer dev-local`, so anyone with network access to port 8000 can use it. Treat your tailnet + LAN as the trust boundary.

---

## Seeding test data

After either path is up, populate the local DB with enough data for end-to-end clicks:

```bash
scripts/seed-local.sh
```

This script is **idempotent** — run it as many times as you want.

It creates:
- A Contact + `staff_admin` role for the `dev-local` B2C subject (so the daily digest's recipient query finds them).
- Two test facilitator B2C subjects with Contact rows + `facilitator_org_membership` rows against the seeded U5 demo orgs (`Example Mission Network` + `Frontier Adoption Alliance`).
- The `Facilitator welcome` drip campaign created + activated, with `send_at_hour=0` so dev sends fire immediately (production seeds set this to 9).
- Three test adopter contacts via `POST /v1/contacts/manual`: one with one FPG, one multi-FPG, one with no FPG (lands in the triage queue).
- A match-run for each adopter so the queue isn't empty.

Override the API / DB endpoints with env vars if you've changed ports:

```bash
API_URL=http://localhost:8001 PG_PORT=15432 scripts/seed-local.sh
```

---

## What works without ACS

The dev stack has no Azure Communication Services credential, and that's fine:

- **Magic-link sign-in**: the worker logs `magic_link.email.dev_fallback recipient=...` and skips the actual send. The token is still persisted; click flow still works if you read the link out of the worker logs.
- **Drip campaign sends**: the worker logs `drip.email.dev_fallback recipient=...` and treats the send as successful so the enrollment state machine still advances. `EnrollmentEvent` rows are written.
- **Daily digest**: same dev fallback. `DigestRecipient` rows are written with `status='sent'`.

To actually exercise B2C sign-in, populate `apps/web/.env.local` with the existing JP B2C tenant values (`NEXT_PUBLIC_AZURE_AD_B2C_*`). Otherwise the sign-in page shows the `dev-local` bearer textbox and you can paste `dev-local` to authenticate.

---

## Daily digest gate (dev workaround)

The `send_daily_digest` worker task gates on `eastern_now.hour == 9 AND minute < 30`. In real time, that's a half-hour window per day. For local testing off-hours:

**Option A**: temporarily relax the gate. Edit `apps/worker/src/jp_adopt_worker/tasks/send_daily_digest.py`, comment out the `if not (eastern_now.hour == 9 ...)` block. **Don't commit this change.**

**Option B**: call `run_digest()` directly from a Python REPL:

```python
# uv run python  (from apps/api/)
import asyncio
from datetime import UTC, datetime, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from jp_adopt_worker.tasks.send_daily_digest import run_digest

async def main():
    engine = create_async_engine("postgresql+asyncpg://jp_adopt:jp_adopt@127.0.0.1:5434/jp_adopt")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            now = datetime.now(UTC)
            counts = await run_digest(
                session,
                window_start=now - timedelta(hours=24),
                window_end=now,
                acs_connection_string=None,
                acs_sender_address="no-reply@local",
            )
        await session.commit()
    print(counts)

asyncio.run(main())
```

---

## Smoke tests

The fastest way to verify the stack:

```bash
scripts/smoke-local.sh
```

Runs 11 checks (healthz, readyz, queue read, drip read, manual create,
match-run, queue re-read, workflow transition, outbox + Match assertions,
worker tick log). Prints PASS/FAIL per check; exits 0 only if all pass.
Idempotent — each run creates a uniquely-suffixed `smoke+<uuid>@example.com`
contact tagged `origin='smoke_test'` for easy SQL cleanup.

Override the API endpoint to test through Tailscale / LAN:

```bash
API_URL=http://your-host.your-tailnet.ts.net:8000 scripts/smoke-local.sh
```

Cleanup smoke contacts when you're done:

```sql
DELETE FROM contacts WHERE origin='smoke_test';
```

### Manual smoke (without the script)

After `scripts/seed-local.sh` runs, these should all succeed:

```bash
# API up
curl -s http://127.0.0.1:8000/healthz | jq .

# Match queue (dev-local sees all)
curl -s -H "Authorization: Bearer dev-local" \
  http://127.0.0.1:8000/v1/matches/queue | jq '.total'
# Expect: integer >= 2

# Drip campaigns
curl -s -H "Authorization: Bearer dev-local" \
  http://127.0.0.1:8000/v1/drips/campaigns | jq '.items[].status'
# Expect: at least one "active"

# Web responding
curl -s http://localhost:3000 | grep -i "JP ADOPT"
```

In a browser:
- http://localhost:3000/matches — should list the three adopters' recommendations
- http://localhost:3000/facilitator — empty unless you sign in as a real facilitator (the dev-local bearer is staff-shaped)
- http://localhost:3000/contacts/new — manual contact form

---

## Resetting to a clean state

```bash
# Full nuke (drops Postgres volume → loses all data)
docker compose --profile full down -v
docker compose --profile full up --build
scripts/seed-local.sh
```

That gets you back to a verified-clean state in ~60 seconds.

---

## Troubleshooting

| Symptom | Probable cause | Fix |
|---------|----------------|-----|
| `pnpm run dev:stack` fails: port 5434 already in use | Local Postgres binding 5434 | `pnpm postgres:down` then retry — OR change docker-compose's host port mapping |
| Web build fails with `Module not found: '@jp-adopt/contracts'` | Contracts not generated | `pnpm contracts:generate` |
| `seed-local.sh` errors: "API not responding" | API hasn't started yet | Wait for `Uvicorn running on http://0.0.0.0:8000` log line, then retry |
| `seed-local.sh` errors: "Neither host psql nor a running 'postgres' compose service is available" | No psql installed AND Docker isn't running | `brew install libpq && brew link --force libpq` OR `docker compose up postgres` |
| Worker logs show `drip.email.acs_sdk_missing` | Azure SDK not installed | Expected in some dev environments. Worker still records EnrollmentEvent + advances state |
| Daily digest never fires | Outside the 9-9:30 ET window | Use the relax-the-gate or REPL workaround above |
| `docker compose --profile full up --build` slow on first run | Initial image build pulls Python + Node base layers | Subsequent rebuilds use BuildKit cache layers (~10x faster) |

---

## Reference

- `docker-compose.yml` — the service definitions
- `apps/api/Dockerfile`, `apps/worker/Dockerfile`, `apps/web/Dockerfile` — image builds
- `scripts/setup-local.sh` — one-time bootstrap
- `scripts/dev-stack.sh` — native dev runner (used by `pnpm run dev:stack`)
- `scripts/seed-local.sh` — populates test data
- `docs/runbooks/spike-local-dev.md` — Phase 0 spike notes (kept for historical context; this file supersedes for the day-to-day flow)
