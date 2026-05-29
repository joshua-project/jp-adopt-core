# jp-adopt-forms → adopt-core import runbook

Operator guide for draining historical (and failed-sync) submissions from
jp-adopt-forms Postgres into adopt-core via the `forms-etl` CLI. Closes the
production data gap before staff user-testing (#39 / issue #75).

## Source schema note

The forms database uses **two normalized tables** — `adoption_submissions` +
`adoption_fpg_selections`, and `facilitation_submissions` +
`facilitation_fpg_selections` — not a single JSONB `submissions` table.
The importer reads both and merges by `created_at`.

## Preflight

1. **Target DB at head migration** (no new adopt-core migration required):
   ```bash
   uv run --package jp-adopt-api alembic current
   ```
2. **Network**: confirm the importer host can reach **both** databases:
   - adopt-core Postgres (production or staging)
   - jp-adopt-forms Postgres (separate ACA env)
   
   Options:
   - **Azure**: run from a one-shot Container App Job or `az containerapp exec`
     on a pod in the managed VNet.
   - **Laptop**: Tailscale + Postgres SSL to both hosts.
3. **Credentials** (read-only on forms, read/write on adopt-core):
   - Forms DB: jp-infrastructure Key Vault / 1Password vault entry for
     `jp-adopt-forms` Postgres connection string.
   - Target DB: standard `JP_ADOPT_*` production connection vars.
4. **FPG cache**: target DB must have `fpg` rows for any `people_id3` codes
   present in forms submissions (same requirement as live intake).

## Dry-run

```bash
uv run --package jp-adopt-etl forms-etl \
  --forms-postgres-url "postgresql+psycopg2://${FORMS_USER}:${FORMS_PASS}@${FORMS_HOST}:5432/jp_adopt_forms" \
  --postgres-url "postgresql+psycopg2://${JP_ADOPT_USER}:${JP_ADOPT_PASS}@${PG_HOST}:5432/jp_adopt" \
  --mode dry_run \
  --verbose 2>&1 | tee /tmp/forms-etl-dryrun-$(date +%Y%m%d).log
```

**Expected:** exit code 0, per-row summary in the log, one new `etl_run` row:

```sql
SELECT id, mode, table_name, rows_in, rows_out_inserted, rows_in_conflict, started_at
FROM etl_run
WHERE table_name = 'submissions' AND mode = 'dry_run'
ORDER BY started_at DESC LIMIT 1;
```

Dry-run **does not** persist contacts or outbox events (savepoint rollback).
Inspect would-be failures:

```sql
SELECT conflict_type, COUNT(*)
FROM migration_conflicts
WHERE source_system = 'jp-adopt-forms'
GROUP BY 1;
```

(Conflicts from dry-run are rolled back — use `--verbose` log output for triage.)

## Production import

```bash
uv run --package jp-adopt-etl forms-etl \
  --forms-postgres-url "postgresql+psycopg2://..." \
  --postgres-url "postgresql+psycopg2://..." \
  --mode production \
  --verbose 2>&1 | tee /tmp/forms-etl-prod-$(date +%Y%m%d).log
```

### Verification queries

```sql
-- Imported contact count (compare to forms source count)
SELECT COUNT(*) FROM contacts WHERE source_system = 'jp-adopt-forms';

-- No duplicate source keys
SELECT source_system, source_id, COUNT(*)
FROM contacts
WHERE source_system = 'jp-adopt-forms'
GROUP BY 1, 2 HAVING COUNT(*) > 1;

-- Exactly one bulk outbox event for this run (not per-row submission events)
SELECT event_type, payload_json->>'label', created_at
FROM outbox
WHERE event_type = 'jp.adopt.v1.bulk_imported'
ORDER BY created_at DESC LIMIT 3;

-- Audit row + watermark for next run
SELECT source_max_modified_at, rows_in, rows_out_inserted, rows_in_conflict
FROM etl_run
WHERE table_name = 'submissions' AND mode = 'production'
ORDER BY started_at DESC LIMIT 1;
```

Spot-check five contacts in the staff UI (`/contacts`) and match queue
(`/matches`). Historical rows should sort by their forms `created_at`
(preserved on insert).

## Re-run / incremental

Read the prior watermark:

```sql
SELECT source_max_modified_at
FROM etl_run
WHERE table_name = 'submissions' AND mode = 'production'
ORDER BY started_at DESC LIMIT 1;
```

Pass it to the next run:

```bash
uv run --package jp-adopt-etl forms-etl \
  ... \
  --mode production \
  --watermark "2024-12-01T18:30:00+00:00"
```

Re-running **without** a watermark is safe: email-keyed dedup in intake
prevents duplicate contacts; `source_system` + `source_id` partial unique
index prevents duplicate source-keyed rows.

## Rollback

If a production import landed bad data, delete by source marker (review first):

```sql
BEGIN;
-- Delete dependent rows first (adjust if FK cascades differ in your env)
DELETE FROM adopter_interest
WHERE contact_id IN (
  SELECT id FROM contacts WHERE source_system = 'jp-adopt-forms'
);
DELETE FROM contact_profile
WHERE contact_id IN (
  SELECT id FROM contacts WHERE source_system = 'jp-adopt-forms'
);
DELETE FROM consent
WHERE contact_id IN (
  SELECT id FROM contacts WHERE source_system = 'jp-adopt-forms'
);
DELETE FROM contacts WHERE source_system = 'jp-adopt-forms';
COMMIT;
```

## Troubleshooting

| `conflict_type` | Meaning | Action |
|---|---|---|
| `validation_error: missing email` | Forms row lacks required email | Fix in forms DB or skip row |
| `validation_error: …` | Pydantic/schema mismatch | Compare row to `core-client.ts` mapping |
| `unknown_form_type` | Unexpected `form_type` | Should not occur — file a bug |
| `validation_failed: Unknown people_id3` | FPG not in adopt-core `fpg` table | Sync FPG cache, re-run |
| `processing_error: …` | Unexpected intake failure | Check API logs; row recorded for review |

Rows in `migration_conflicts` do not stop the run; review after each import.
