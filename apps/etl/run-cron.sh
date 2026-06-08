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

exec dt-etl \
    --mysql-url "$DT_MYSQL_URL" \
    --postgres-url "$PG_URL" \
    --table all \
    --mode production \
    --watermark auto \
    --verbose
