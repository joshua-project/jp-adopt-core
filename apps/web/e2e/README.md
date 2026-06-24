# Browser walkthrough e2e

`walkthrough.mjs` drives a real Chromium through every staff page plus the
drip-editor flow (type → insert merge token → send test), capturing console
errors, uncaught exceptions, 5xx API calls, the nav-overlap regression, and
full-page screenshots. It exits non-zero on any **hard** finding
(`UNCAUGHT`, `API5XX`, `NAV`, `BUG`).

It exists because unit/component tests pass in jsdom while the live app can
still be broken — the drip send-test `503` and the nav overlapping the brand
both shipped green and were only caught by clicking through the running app.

## Run it

Needs a running local stack (web + API + seeded DB). The app must be in dev
mode (no `NEXT_PUBLIC_AZURE_AD_*`) so it auto-auths as `dev-local`.

```bash
# 1. bring the stack up + seed (see repo AGENTS.md)
pnpm run dev:stack            # API :8000, web :3000
scripts/seed-local.sh         # campaigns, contacts, matches

# 2. one-time browser install
pnpm --filter web exec playwright install chromium

# 3. run the walkthrough
pnpm --filter web walkthrough
```

Screenshots + `findings.json` land in `apps/web/e2e-shots/` (gitignored).

### Env overrides

| var | default | purpose |
|-----|---------|---------|
| `E2E_BASE_URL` | `http://localhost:3000` | web app base |
| `E2E_API_URL`  | `http://localhost:8000` | API base (ID discovery) |
| `E2E_SHOTS_DIR`| `e2e-shots`              | screenshot output dir |

Campaign/contact/match IDs are discovered from the API at runtime, so it works
against any seeded environment.

## CI

Not yet wired into CI — it needs the full stack (Postgres + Redis + API + web +
seed) stood up in the job. Tracked as a follow-up; run it locally before
shipping drip/UI changes in the meantime.
