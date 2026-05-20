# jp-adopt-etl

Batch ETL for migrating Disciple.Tools (DT) MySQL data into the
jp-adopt-core Postgres schema (U9 of the Amy-return build).

Idempotent — every imported row carries `(source_system='dt', source_id=…)`
and uses `INSERT … ON CONFLICT DO UPDATE … WHERE local_modified_after_import
= false`. Re-runs skip rows that staff have edited in the new system.

Sync stack on purpose. The API service is asyncpg + asyncio; this batch
job is straight psycopg2 + pymysql so the ETL doesn't need to mix
concurrency models. Mappers are pure Python functions exercising no I/O.

## Usage

```bash
# Dry run against fixture data (no writes; produces an etl_run row with mode='dry_run')
uv run --package jp-adopt-etl dt-etl \
  --mysql-url mysql+pymysql://user:pass@host:3306/dt \
  --postgres-url postgresql+psycopg2://jp_adopt:jp_adopt@localhost:5434/jp_adopt \
  --table contacts \
  --dry-run

# Production cutover (writes; emits a single jp.adopt.v1.bulk_imported event via outbox suppression)
uv run --package jp-adopt-etl dt-etl \
  --mysql-url mysql+pymysql://... \
  --postgres-url postgresql+psycopg2://... \
  --table all

# Delta against last successful watermark
uv run --package jp-adopt-etl dt-etl \
  --mysql-url ... --postgres-url ... \
  --table contacts \
  --watermark
```

See `docs/runbooks/dt-cutover.md` for the Saturday 5/23 14:00 ET cutover sequence.
