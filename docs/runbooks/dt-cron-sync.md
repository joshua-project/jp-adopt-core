# DT delta cron — hourly sync

The hourly cron keeps `jp-adopt-core` mirrored against legacy DT MySQL
until the eventual cutover. It complements (does not replace) the
manual `--mode dry_run` rehearsal documented in `docs/runbooks/dt-cutover.md`.

## What it is

- **Azure Container Apps Job** `jp-adopt-etl-cron-production` in
  resource group `rg-jp-adopt-core-production`.
- **Image** `jp-adopt-etl` (`apps/etl/Dockerfile`), built and pushed by
  the deploy workflow (`build-etl` job in `.github/workflows/deploy.yml`).
- **Schedule** `0 * * * *` — top of every UTC hour. Azure Container Apps
  Job cron expressions are evaluated in **UTC** regardless of the
  container's `TZ` env var. The `TZ=America/New_York` in the Dockerfile
  only affects the running process (e.g. `datetime.now()` and log
  timestamps inside the ETL), NOT the scheduler. Hourly cadence means
  the timezone choice doesn't affect business logic, but operators
  comparing log timestamps to scheduled fires should expect the cron's
  invocation timestamps in UTC.
- **Command** `./run-cron.sh` → `dt-etl --table all --mode production
  --watermark auto --verbose` (see `apps/etl/run-cron.sh`).
- **Watermark** the previous successful run's `MIN(MAX(etl_run.source_max_modified_at))`
  per table. Resolved inside the orchestrator's
  `resolve_auto_watermark()`. First run after deploy: no prior
  successful runs ⇒ full scan.

## Why a Container Apps Job

- Scheduled execution as a first-class Azure concept (no cron container
  to babysit, no ARQ task to share a worker with).
- Replica-level retries via `--replica-retry-limit`.
- Cleanly separated image so the API/web/worker images don't pick up
  the ETL's psycopg2/pymysql dependencies.

## Prereqs (one-time)

1. Migrations `0023`, `0024`, `0025` applied on the prod jp-adopt-core
   Postgres. `0025` (partial unique index on `migration_conflicts`) is
   what makes the cron's per-run conflict-record path idempotent.
2. FPG table seeded via `sync_fpg.py` (otherwise the per-hour run will
   report ~800+ `fpg_not_found` conflicts).
3. **Firewall**: the ACA prod env's outbound IP `20.15.145.82/32`
   allowlisted on:
   - `jp-mysql-flex-production` (DT MySQL) — rule
     `adopt-core-api-outbound`.
   - `jp-postgresql-production` (target) — implicit via the existing
     `AllowAzureServices` rule on the flexible server.
   - `jp-adopt-forms-production` access restrictions — rule
     `adopt-core-api-outbound` (the API container uses this for
     `sync_fpg`, not the ETL, but the rule is shared).
4. 1Password item `Adopt Core - Production` has `aca-environment-name`
   populated.

## Operating the job

### Start an out-of-schedule run (manual cutover rehearsal, etc.)

```bash
az containerapp job start \
  --name jp-adopt-etl-cron-production \
  --resource-group rg-jp-adopt-core-production
```

This runs the same `run-cron.sh` command. Watermark resolution is
unchanged — the manual run still respects `etl_run` history.

### Override args (full re-baseline)

```bash
az containerapp job start \
  --name jp-adopt-etl-cron-production \
  --resource-group rg-jp-adopt-core-production \
  --image "${ACR}/jp-adopt-etl:latest" \
  --command "dt-etl" \
  --args "--mysql-url \$DT_MYSQL_URL --postgres-url \$DATABASE_URL --table all --mode production --verbose"
```

(Note the absence of `--watermark` — forces full scan.)

### Inspect a run

```bash
# List recent executions
az containerapp job execution list \
  --name jp-adopt-etl-cron-production \
  --resource-group rg-jp-adopt-core-production \
  --output table

# Tail logs for the most recent execution
az containerapp job logs show \
  --name jp-adopt-etl-cron-production \
  --resource-group rg-jp-adopt-core-production \
  --follow
```

### Disable / re-enable

```bash
az containerapp job update \
  --name jp-adopt-etl-cron-production \
  --resource-group rg-jp-adopt-core-production \
  --trigger-type Manual
```

Setting `--trigger-type Manual` disables the schedule. Restore with
`--trigger-type Schedule --cron-expression "0 * * * *"`.

## Verification queries (run any time)

Both SQL (direct DB access) and API (staff_admin-gated, agent-callable)
shapes are supported. The API endpoints query the same three tables but
require no firewall rule or Postgres credentials:

```http
GET /v1/admin/etl-runs?has_errors=false&limit=10
GET /v1/admin/migration-conflicts?summary=true
GET /v1/admin/etl-deleted-in-source
```

The `summary=true` query param on `migration-conflicts` returns aggregate
counts grouped by `(table_name, conflict_type)` — directly equivalent to
the `SELECT conflict_type, COUNT(*) GROUP BY 1` shape below. Other
filters: `?source_system=`, `?table_name=`, `?conflict_type=`, `?since=`
(ISO 8601). For etl-runs: `?mode=`, `?has_errors=true|false`, `?since=`.

For agents using a Bearer token, no further setup is needed. For raw
HTTP from a script, hit the production API at `<api-base>/v1/admin/...`
with `Authorization: Bearer <token>`.

```sql
-- Last successful run per table
SELECT table_name, MAX(started_at) AS last_run,
       MAX(source_max_modified_at) AS watermark_now
FROM etl_run
WHERE mode = 'production' AND errors = 0
GROUP BY table_name
ORDER BY 1;

-- Cron health: any run with errors in the last 4 hours?
SELECT table_name, started_at, errors, rows_in_conflict
FROM etl_run
WHERE mode = 'production'
  AND started_at > now() - interval '4 hours'
  AND errors > 0
ORDER BY started_at DESC;

-- Conflict accumulation check — migration 0025's partial unique index
-- means each (source_id, conflict_type) pair appears at most once,
-- even after many cron runs. If the count grows linearly with runs,
-- the dedup index is missing or the conflict_type is varying.
SELECT conflict_type, COUNT(*) AS total
FROM migration_conflicts
WHERE source_system = 'dt'
GROUP BY 1
ORDER BY 2 DESC;
```

## Common conflict types and resolution

| Type | What it means | Resolution |
|---|---|---|
| `local_modified_after_import` | A staff member edited the contact in jp-adopt-core after a prior import. ETL preserved the local edit. | Expected. Reconcile post-cutover by deciding which value wins per case. |
| `local_assignment_override` | Staff reassigned the contact in jp-adopt-core after a prior import; DT still has the old assignee. | Expected. The cron will NOT clobber the staff reassignment. |
| `assignee_no_subject` | DT `assigned_to` resolves to a `dt_user_id` that has no B2C subject. | Auto-resolves the hour the staff member first signs into jp-adopt-core. |
| `duplicate_email` | DT contact's email matches an existing contact in jp-adopt-core (likely from intake forms). | Reconcile manually — typically merge the DT history into the forms contact. |
| `fpg_not_found` | DT `fpg_submission_data` references a `peopleId3` not in the `fpg` table. | Verify the FPG seed is current (re-run `sync_fpg`). Truly absent peopleId3 values are typos or stale references in DT data. |
| `unmapped_status:*` | DT enum value not in `mappers/status.py`. | Add the mapping in `apps/etl/src/jp_adopt_etl/mappers/status.py`, deploy. The cron in production mode records `unknown` + the conflict; dry-run fails loud. |

## Failure modes

| Symptom | Diagnosis | Recovery |
|---|---|---|
| Job execution status = `Failed`, exit code 1 | Connection / auth / unmapped status. Inspect logs. | Fix root cause; replay via `az containerapp job start`. The next scheduled run will also retry. |
| `etl_run.errors > 0` in verification query | One or more table imports raised mid-run. | Open the job execution log for the matching `started_at`. The dry-run audit-replay pattern does NOT apply to production runs — partial state is rolled back. |
| Watermark not advancing | `resolve_auto_watermark` returns a value but no rows are imported. | Source data is stable. Verify by inspecting `source_max_modified_at` on the latest `etl_run` row vs DT MySQL's `MAX(post_modified_gmt)`. |
| Cron running but no `etl_run` rows appear | Job is failing before the first per-table block. | Check the job's secret bindings (`DT_MYSQL_URL`, `DATABASE_URL`). |

## Cutover plan integration

When the actual DT → jp-adopt-core cutover happens (per
`docs/runbooks/dt-cutover.md`), disable the cron with
`--trigger-type Manual` during the freeze window, take the final
snapshot, then delete the job entirely after the cutover succeeds.
