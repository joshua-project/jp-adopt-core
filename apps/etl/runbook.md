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
`staff_identity_link` (both added in 0009). Migration `0024` adds the
same shape to `adopter_interest` (`uq_adopter_interest_source_system_source_id`).

## Duplicate emails (DT) vs partial unique index (jp-adopt-core)

DT permits the same email on multiple contacts; jp-adopt-core has a
partial unique index on `contacts.email_normalized` and does not. The
orchestrator detects the collision pre-insert by loading the existing
`(email_normalized → (source_system, source_id))` map, claiming each
email for the first DT contact that submits it, and for subsequent
contacts that try to claim the same email:

1. Keep the contact row (so notes/interests/assignments still attach).
2. Clear `email_normalized` on the colliding row.
3. Record a `migration_conflicts` row with
   `conflict_type='duplicate_email'` and `source_value={"email_normalized": "..."}`.

Operators reconcile post-cutover by picking the canonical contact per
email and re-attaching the duplicate's history if needed.

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
| Side effects | **Non-mutating.** All data writes (and the suppressed `bulk_imported` outbox row) are rolled back at the end; only the `etl_run` audit rows are committed so the operator still gets a per-table summary. | Commits inserts/updates |

`dry_run` is both fail-loud-on-unknowns AND write-free: it runs the full
import against the target DB inside one transaction, then rolls back the
data and re-commits just the `etl_run` rows. Safe to point at the real
target for a rehearsal — it leaves no `source_system='dt'` rows behind.

## Tables and order

The orchestrator runs tables in dependency order so foreign-key
references resolve. `--table all` is shorthand for:

1. `staff_identity_link` (wp_users) — required before assignment + author
   resolution.
2. `contacts` (wp_posts + wp_postmeta) — also populates `contact_profile`
   and `adopter_interest` (from `fpg_submission_data` JSON) inline.
3. `contact_assignment` — depends on contacts + staff_identity_link; an
   assignee without a B2C subject records `assignee_no_subject`.
4. `activity_log` — wp_comments (notes/emails) and `wp_dt_activity_log`
   (field-change history) merged into a single target table.
5. `adopter_interest` — re-runs against `fpg_submission_data` are
   idempotent via `(source_system, source_id='<contact>:<peopleId3>')`.

On full (non-watermark) contact runs the orchestrator also writes
`etl_deleted_in_source` rows for contacts that were present on a prior
run but absent from the current snapshot — no hard delete.

## Delta vs full ETL

The `--watermark` flag filters source rows to those modified after the
supplied ISO 8601 timestamp:

- For `contacts`: `wp_posts.post_modified_gmt > watermark`.
- For `activity_log` (comments): `wp_comments.comment_date_gmt > watermark`.
- For `activity_log` (field changes): `wp_dt_activity_log.hist_time > epoch(watermark)`.
- For `staff_identity_link`: no watermark (full scan; wp_users is
  small).
- For `contact_assignment`: no watermark (1:1 with contacts; re-resolves
  on each run).
- For `adopter_interest`: no watermark; gated by the contact's
  watermark (interests parse from the contact's postmeta).

The previous run's `etl_run.source_max_modified_at` is the next run's
watermark. The orchestrator stores this at the end of each table's
import.

## Failure modes

| Symptom | Diagnosis | Recovery |
|---------|-----------|----------|
| `UnmappedStatusError` | New DT enum value | Add to `mappers/status.py`, rebuild, re-run |
| `IntegrityError: uq_contacts_email_normalized` | Two DT contacts share an email | Handled — see "Duplicate emails" above; not a failure mode |
| `InvalidColumnReference: no unique constraint` | Migration 0024 not applied | `uv run alembic upgrade head` |
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
- `tests/test_channels_mapper.py` — `contact_email_<hash>` /
  `contact_phone_<hash>` extraction with verified-first ordering.
- `tests/test_profile_mapper.py` — ~30 ContactProfile fields with
  type coercion and CHECK domain clamping.
- `tests/test_interests_mapper.py` — `fpg_submission_data` JSON parse.
- `tests/test_activity_history_mapper.py` — `wp_dt_activity_log`
  field-change → `kind='field_change'` rendering.
- `tests/test_assignment_mapper.py` — `assigned_to` (`user-<id>`) parse.
- `tests/test_comments_mapper.py` — comment → activity_log + author
  resolution, including the legacy-unknown sentinel.
- `tests/test_users_mapper.py` — wp_users → staff_identity_link.
- `tests/test_orchestrator_integration.py` — end-to-end with a mocked
  MySQL source against real Postgres. Covers user import (idempotent),
  contact import (postmeta pivot + outbox suppression integration),
  `local_modified_after_import` skip path, contact_profile populated
  from JP-custom postmeta, `fpg_submission_data` interest fan-out,
  comment threading parent_id resolution, field-change activity_log,
  assignment with conflict for unmapped assignees, non-mutating
  dry_run, full-run deletion tracking, and duplicate-email collision
  handling.

## Out of scope in v1

- Match row import: the matching algorithm in U6 owns Match creation;
  we don't backfill historical DT matches in v1 (Amy will re-run the
  matcher on imported contacts post-cutover).
- Rate-limiting / chunked progress: the orchestrator runs sequentially.
  For >50K contact installations, add chunk-level progress logging.
