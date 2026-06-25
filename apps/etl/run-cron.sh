#!/usr/bin/env sh
#
# Entrypoint for the dt-etl-cron Container Apps Job.
#
# Reads connection URLs from the env (DT_MYSQL_URL, DATABASE_URL) and
# invokes dt-etl with --watermark auto so each hourly run picks up where
# the prior one left off. The DATABASE_URL from the API/worker container
# apps is asyncpg-flavoured (postgresql+asyncpg://); the ETL needs the
# sync psycopg2 driver, so we coerce the scheme.
#
# Exit code 0 → success. Anything else surfaces as a Container Apps Job
# replica failure, which triggers ACA's retry policy and shows in the
# job execution history. Slack-on-failure is wired via deploy.yml's
# Application Insights / Log Analytics alert (separate from this script).

set -eu

: "${DT_MYSQL_URL:?DT_MYSQL_URL must be set}"
: "${DATABASE_URL:?DATABASE_URL must be set}"

# Coerce asyncpg → psycopg2 for the ETL's sync stack. Accept both the
# bare 'postgresql://' shape (some env shapes don't include the driver)
# and 'postgresql+asyncpg://' (what the API container apps use).
PG_URL=$(echo "$DATABASE_URL" | sed \
    -e 's|^postgresql+asyncpg://|postgresql+psycopg2://|' \
    -e 's|^postgresql://|postgresql+psycopg2://|')

dt-etl \
    --mysql-url "$DT_MYSQL_URL" \
    --postgres-url "$PG_URL" \
    --table all \
    --mode production \
    --watermark auto \
    --verbose

# After the delta sync, reconcile the duplicate_email conflicts it just
# recorded. DT allows the same email on multiple contacts; the new system's
# partial unique index does not, so when a synced DT contact carries an email
# already owned by an existing (e.g. forms-intake) contact, the importer keeps
# the row but NULLs the colliding email and records a duplicate_email conflict.
# Track A merges those DT-authoritatively onto the email owner — but only the
# high-confidence name+email matches; ambiguous cases stay as conflict rows for
# Amy to review. Running it here (a separate process, AFTER the outbox-suppressed
# sync) means each merge's effect is captured in its own outbox summary.
# Idempotent: a run with no new conflicts is a no-op. set -e above already
# aborts before this on a failed sync, so we never reconcile a half-synced state.
#
# --decisions-from-db reads the duplicate_review_decision table (the staff
# "Review duplicates" UI), so a reviewer's "same person → merge" call is applied
# on the next run.
exec dt-reconcile-track-a \
    --mysql-url "$DT_MYSQL_URL" \
    --postgres-url "$PG_URL" \
    --apply \
    --decisions-from-db
