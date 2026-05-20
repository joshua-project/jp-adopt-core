# jp-adopt-etl runbook (U9)

Implementation reference for the DT MySQL → jp-adopt-core Postgres ETL.
See `docs/runbooks/dt-cutover.md` for the operator-facing cutover
sequence; this document covers the ETL's design and operational quirks.

## Architecture

```
DT MySQL                      jp-adopt-core Postgres
─────────                     ──────────────────────
wp_posts (post_type='contacts') ─┐
                                 │
wp_postmeta (EAV)               ─┤  apps/etl/orchestrator.py
                                 ├─►  • Reads source via sync pymysql
wp_comments                     ─┤      (dt_source.py)
                                 │  • Pure mapper functions
wp_users                        ─┤      (mappers/*.py)
                                 │  • Writes target via sync psycopg2
wp_p2p (contacts_to_peoplegroups) ┘     (ORM models from jp-adopt-api)
                                    • Wrapped in outbox_suppressed()
                                      so one bulk_imported event fires
                                      per run (not per row)
```

## Sync stack on purpose

The API service is asyncpg + asyncio. The ETL is sync (pymysql +
psycopg2). The two stacks share ORM model definitions but never share a
session. The mixed `outbox_suppressed` wrapper around the sync work is
acceptable because the suppression bookkeeping uses `contextvars` which
propagate cleanly through the asyncio Task boundary; the only async-aware
operation is the context manager's enter/exit.

If you find yourself needing async ETL — DON'T. The batch shape is
sequential by intent (read source, transform, write target). The sync
stack is simpler to reason about during cutover when the operator may be
piping output through `tee`, attaching a debugger, or running pieces by
hand.

## CLI

The console entry point is `dt-etl`, exposed via the workspace as:

```bash
uv run --package jp-adopt-etl dt-etl --help
```

Flags:

| Flag | Required | Description |
|------|----------|-------------|
| `--mysql-url` | yes | SQLAlchemy URL for the DT MySQL source (`mysql+pymysql://…`) |
| `--postgres-url` | yes | SQLAlchemy URL for the target Postgres (`postgresql+psycopg2://…`) |
| `--table` | no | Repeat per table, or use `all` (default). Tables run in dependency order. |
| `--mode` | no | `dry_run` (default) fails loudly on unmapped values; `production` maps to `unknown` + records to `migration_conflicts`. |
| `--watermark` | no | ISO-8601 timestamp; only import rows modified after this point. Use the previous run's `etl_run.source_max_modified_at`. |
| `--batch-size` | no | Default 500. wp_postmeta lookup chunk size. |
| `--verbose` | no | Set root logger to DEBUG. |

## Idempotency

Every imported row carries `(source_system='dt', source_id=<wp_id>)`.
Idempotent upsert via `INSERT … ON CONFLICT (source_system, source_id)
DO UPDATE SET … WHERE local_modified_after_import = false`:

- First run: row inserted.
- Re-run, no local edits: row updated.
- Re-run, staff edited the row in the new system: row SKIPPED;
  `migration_conflicts` records `conflict_type='local_modified_after_import'`
  with the source value for Amy's review.

The `local_modified_after_import` flag is set to `true` by any
PATCH/UPDATE through the API post-import. The ETL writes it as `false`
on insert; the flag stays `true` once set.

## Idempotency on contacts requires a partial unique index

Migration 0009 adds:

```sql
CREATE UNIQUE INDEX uq_contacts_source_system_source_id
  ON contacts (source_system, source_id)
  WHERE source_system IS NOT NULL AND source_id IS NOT NULL;
```

The `ON CONFLICT` clause must include `INDEX WHERE` matching the
partial index predicate. SQLAlchemy's `on_conflict_do_update` exposes
this via `index_where=text("source_system IS NOT NULL AND source_id IS NOT NULL")`.

Equivalent partial indexes apply to `activity_log` and
`staff_identity_link` (both added in 0009).

## Outbox suppression

The orchestrator wraps the entire run in `outbox_suppressed()`. Without
suppression, importing 50K contacts would produce 50K `contact.created`
events, hammering the worker drain and any subscribed webhooks.

With suppression: a single `jp.adopt.v1.bulk_imported` Outbox row is
written at run end carrying `event_counts` (count of suppressed events
by type), `total_suppressed_events`, `duration_seconds`, and the
caller-supplied `metadata` (mode, tables, scheme, timestamps).

The current orchestrator does NOT call `emit_outbox` from the mappers —
the imports use ORM models directly. The suppression context still
emits its summary row at exit. As mappers grow (U13 may add explicit
events from the ETL itself), they will route through `emit_outbox` and
be captured in the per-event counts.

## Dry-run vs production

`--mode dry_run` and `--mode production` differ on three behaviors:

| | dry_run | production |
|---|---|---|
| Unmapped DT status | Raises `UnmappedStatusError` | Maps to `'unknown'` + `migration_conflicts` row |
| etl_run.mode | `'dry_run'` | `'production'` |
| Side effects | **CURRENTLY:** same as production. v1 commits all writes regardless of mode. To do a non-mutating dry run, use a read-only Postgres user or run against a throwaway DB. | Commits inserts/updates |

The v1 dry-run is fail-loud-on-unknowns, not write-free. This is a known
gap — `docs/runbooks/dt-cutover.md` calls it out and recommends staging
as the dry-run target.

## Delta vs full ETL

The `--watermark` flag filters source rows to those modified after the
supplied ISO 8601 timestamp:

- For `contacts`: `wp_posts.post_modified_gmt > watermark`.
- For `activity_log`: `wp_comments.comment_date_gmt > watermark`.
- For `staff_identity_link`: no watermark (full scan; wp_users is
  small).
- For `adopter_interest`: not implemented in v1 (p2p resolution
  deferred to U13).

The previous run's `etl_run.source_max_modified_at` is the next run's
watermark. The orchestrator stores this at the end of each table's
import.

## Failure modes

| Symptom | Diagnosis | Recovery |
|---------|-----------|----------|
| `UnmappedStatusError` | New DT enum value | Add to `mappers/status.py`, rebuild, re-run |
| `OperationalError: wp_p2p` not found | Older DT install without p2p plugin | Warned + skipped; AdopterInterest deferred to U13 |
| `InvalidColumnReference: no unique constraint` | Migration 0009 not applied | `uv run alembic upgrade head` |
| `InFailedSqlTransaction` after an error | Previous statement failed, transaction is aborted | The orchestrator handles per-table; investigate `etl_run.errors > 0` |
| `migration_conflicts` row with `conflict_type='local_modified_after_import'` | Staff edited the contact between runs | Expected — review post-cutover |

## Local development

```bash
# 1. Workspace sync (adds apps/etl to the uv workspace)
uv sync --package jp-adopt-etl --extra dev

# 2. Run the test suite
cd apps/etl && uv run --extra dev pytest -q

# 3. Run against a local MySQL fixture (requires manual MySQL setup)
uv run --package jp-adopt-etl dt-etl \
  --mysql-url "mysql+pymysql://root:root@127.0.0.1:3306/dt_fixture" \
  --postgres-url "postgresql+psycopg2://jp_adopt:jp_adopt@127.0.0.1:5434/jp_adopt" \
  --table all \
  --mode dry_run --verbose
```

## Test coverage

- `tests/test_status_mapper.py` — pure-function tests for the status
  enum map; every known transition exercised plus dry_run-fails-loud.
- `tests/test_contacts_mapper.py` — pivot + map tests including
  phpserialize roundtrip and the synthetic-display-name fallback.
- `tests/test_comments_mapper.py` — comment → activity_log + author
  resolution, including the legacy-unknown sentinel.
- `tests/test_users_mapper.py` — wp_users → staff_identity_link.
- `tests/test_orchestrator_integration.py` — end-to-end with a mocked
  MySQL source against real Postgres. Covers user import (idempotent),
  contact import (postmeta pivot + outbox suppression integration),
  and the `local_modified_after_import` skip path.

## Out of scope in v1

- AdopterInterest rop3 resolution (p2p_to → wp_postmeta → rop3): the
  reader is in place but the mapper records a `migration_conflicts` row
  rather than attempting the lookup. U13 cutover handles this.
- Match row import: the matching algorithm in U6 owns Match creation;
  we don't backfill historical DT matches in v1 (Amy will re-run the
  matcher on imported contacts post-cutover).
- Rate-limiting / chunked progress: the orchestrator runs sequentially.
  For >50K contact installations, add chunk-level progress logging.
