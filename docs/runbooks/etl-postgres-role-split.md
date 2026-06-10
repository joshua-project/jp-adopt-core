# Split the ETL cron onto its own Postgres role (least-privilege)

Today the hourly DT ETL Container Apps Job uses the same `DATABASE_URL`
as the API container app, meaning the cron has the full DML surface the
API needs (read/write/delete on every domain table). Operational risk: a
bug in the ETL or a compromised cron container could delete contacts,
matches, audit rows, etc. This runbook splits the cron onto a dedicated
Postgres role that has only what the ETL needs.

**This work requires Postgres admin access. The PR portion is small
(deploy.yml secret-ref swap + this runbook). The role + grants are an
operator one-time SQL block.**

## Step 1 — Create the ETL role + grants

Connect as the Postgres admin (e.g. via 1Password "Adopt Core - Production"
field `db-url-admin` or the original Terraform-output superuser). Run:

```sql
-- Create the role + password. Pick the password via:
--   openssl rand -base64 48 | tr -d '/+=' | head -c 32
CREATE ROLE jp_adopt_etl LOGIN PASSWORD '<generated>';

-- Read access to every table the ETL reads.
GRANT USAGE ON SCHEMA public TO jp_adopt_etl;

-- The 5 tables the ETL INSERTs / ON CONFLICT UPDATEs into.
GRANT SELECT, INSERT, UPDATE ON TABLE
    contacts,
    contact_profile,
    contact_assignment,
    activity_log,
    adopter_interest,
    staff_identity_link
TO jp_adopt_etl;

-- Audit tables — INSERT + UPDATE (for etl_run.ended_at) + SELECT (the
-- ETL queries etl_run for the auto watermark). No DELETE.
GRANT SELECT, INSERT, UPDATE ON TABLE
    etl_run,
    migration_conflicts,
    etl_deleted_in_source
TO jp_adopt_etl;

-- Outbox: the ETL writes the bulk_imported summary event via
-- outbox_suppressed.
GRANT SELECT, INSERT ON TABLE outbox TO jp_adopt_etl;

-- Reference data the ETL reads to resolve FPGs.
GRANT SELECT ON TABLE fpg TO jp_adopt_etl;

-- pg_locks read for operational debugging (advisory lock visibility).
-- pg_try_advisory_lock itself doesn't need a grant.
GRANT pg_read_all_stats TO jp_adopt_etl;
```

**Conspicuously NOT granted:**

- `DELETE` on any table — the ETL never hard-deletes (the
  `etl_deleted_in_source` audit table is the soft-delete tracker).
- Access to `match`, `match_attempt`, `match_review`, `drip_*`,
  `auth_*`, `intake_*`, `magic_link_*` — domain tables the ETL has no
  business touching.
- `CREATE` / `ALTER` on anything — the ETL doesn't run DDL.
- `SUPERUSER` / `CREATEROLE` / `BYPASSRLS`.

## Step 2 — Store the URL in 1Password

In `Adopt Core - Production`, add a CONCEALED field:

- **Field name:** `db-url-etl`
- **Value:** `postgresql+asyncpg://jp_adopt_etl:<password>@<host>:5432/<db>`

(The `+asyncpg` scheme is correct here — `apps/etl/run-cron.sh` coerces
to `+psycopg2` at startup. Future API/worker callers that reuse the
field shouldn't have to remember.)

## Step 3 — Switch the deploy workflow

In `.github/workflows/deploy.yml`, find the `deploy-etl-job` step and
change the `DATABASE_URL` line:

```diff
-          DATABASE_URL: op://JP Adopt Platform/Adopt Core - Production/database-url
+          DATABASE_URL: op://JP Adopt Platform/Adopt Core - Production/db-url-etl
```

Push and let the deploy fire. The next scheduled cron run uses the new
role.

## Step 4 — Verify

```bash
# Connection works from the cron's outbound IP (already allowlisted)
az containerapp job start \
  --name jp-adopt-etl-cron-production \
  --resource-group rg-jp-adopt-core-production

# Wait ~30s, then check etl_run for an errors=0 entry:
psql "$DATABASE_URL_ADMIN" -c "
  SELECT table_name, errors, ended_at
  FROM etl_run
  WHERE mode='production'
  ORDER BY started_at DESC LIMIT 5;
"
```

If the run reports `permission denied for table <X>`, add the missing
grant via Step 1's pattern. The 5 INSERT/UPDATE tables and 3 audit
tables above are the complete list as of `0025_migration_conflict_dedup`
(see `apps/api/alembic/versions/`).

## Rollback

Revert the deploy workflow change to use `database-url` again; the API's
broader role still has DML on all the same tables, so the next cron fire
succeeds. Leave the `jp_adopt_etl` role in place — it's reusable.

## What this does NOT cover

- The local dev / staging environments still use the broad
  `database-url`. Splitting those is lower-leverage (no real data) and
  is a separate runbook.
- The matching algorithm / drip worker / API itself continue to use
  `database-url` with full DML. Splitting those follows the same
  pattern but per service.
- DT MySQL credentials (the `dt_platform_app` user the cron reads from
  the legacy source) are already least-privilege (SELECT-only role on
  DT side). Documented in `dt-mysql-access` memory.
