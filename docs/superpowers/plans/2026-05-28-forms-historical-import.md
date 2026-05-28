---
status: active
created: 2026-05-28
type: feat
issue: 75
related_issues: [39]
---

# feat: Import historical jp-adopt-forms submissions into adopt-core

Closes #75. Prerequisite for the #39 user-testing keystone — without historical adopter and facilitator records in production Postgres, Amy's testing pass has nothing to triage. Reuses the existing `apps/etl/` package as the home for "batch import from a non-adopt-core source system" so the new forms-import CLI shares the watermark + audit + outbox-suppression machinery that the DT importer (`dt-etl`) already validated in the May cutover.

## Problem frame

Production Postgres currently holds only:

- Two `staff_admin` users (Joel + Amy, seeded via Alembic migrations 0014/0015).
- The three test adopters seeded by `scripts/seed-local.sh` — and even those are dev-local-only, not in production.

The match queue, contacts list, and pipeline kanban are essentially empty in production. Amy can't meaningfully test the staff UI without real records to triage.

Meanwhile, jp-adopt-forms (separate repo, separate ACA env, separate Postgres) has been accepting public adoption + facilitation submissions into its own `submissions` table (JSONB payloads). Two failure modes leave rows there but not in adopt-core:

1. Submissions made before adopt-core's `/v1/intake/*` endpoints were wired into the forms client.
2. Submissions where the live POST to adopt-core failed (network, schema mismatch, transient API error) and the forms-side row was preserved without a successful round-trip.

The fix is the same in both cases: a re-runnable batch importer that drains the forms `submissions` table into adopt-core via the existing intake processing path, idempotently, with no drips or webhooks firing for historical rows.

## Scope

### In scope

- A new `forms-etl` CLI under `apps/etl/`, parallel to the existing `dt-etl`. Shares the `EtlRun`, `MigrationConflict`, and `outbox_suppressed()` infrastructure.
- Read-only source connection to jp-adopt-forms' Postgres `submissions` table (and `submission_audit` if needed for created_at provenance).
- Translation from a forms submission JSONB row to the existing `AdoptionIntake` / `FacilitationIntake` Pydantic schemas.
- In-process call to the canonical intake processing logic (`_process_adoption` / `_process_facilitation`) wrapped in `outbox_suppressed()`. Reuse, do not reimplement, per the issue's mandate.
- Preserve forms' `created_at` on the resulting `Contact` row so historical adopters don't flood the match queue as if they arrived today and drip eligibility windows compute correctly.
- Idempotency via the existing `uq_contacts_source_system_source_id` partial unique index — re-running the importer is safe.
- Dry-run mode (no writes; produces an `etl_run` row with `mode='dry_run'`).
- Watermark support so subsequent runs only process new submissions.
- Operator runbook (`docs/runbooks/forms-data-import.md`) covering preflight, dry-run, production, verification, and re-run procedures.

### Deferred to Follow-Up Work

- **Streaming / scheduled re-import.** v1 is operator-triggered. A scheduled ARQ job that polls forms' Postgres on a cadence is a clean follow-up once the manual import is stable.
- **Two-way reconciliation.** This is a one-way import (forms → core). If a forms row was already round-tripped successfully, the importer's ON CONFLICT DO NOTHING preserves the existing adopt-core row — no reconciliation logic for divergent state.
- **Forms-side cleanup.** Marking imported forms rows as "ingested" in the forms Postgres is out of scope here; the forms repo can read adopt-core's `(source_system='jp-adopt-forms', source_id=…)` rows to compute its own ingested-set.
- **Dual-submit to DT.** No new code that writes to the legacy Disciple.Tools MySQL. DT is cold post-cutover (per `docs/runbooks/dt-cutover.md`); the `apps/etl/dt-etl` path was a one-shot.

### Outside this product's identity

- **Self-service import UI.** This is operator-grade, not a staff feature.
- **Per-row re-try queue.** Failed rows write to `migration_conflicts` for operator review; no automated re-try.

## Dependencies

- **Cross-env network access** from the importer host to jp-adopt-forms' Postgres. Forms runs in a separate ACA env. Options (operator decision at runtime):
  - Run the importer from inside Azure (one-shot ACA `az containerapp exec` or a Container App Job) so both DBs are reachable on the managed VNet.
  - Run from Joel's laptop with Tailscale + Postgres SSL.
  Either works; the runbook (U5) documents both. No code change differs between them.
- **Read-only credentials for forms' Postgres.** Operator obtains via the existing jp-infrastructure Key Vault pattern (or one-shot per-run credential). Not a code dependency.
- **No DB migration in adopt-core.** All target tables (`contacts`, `adopter_interest`, `contact_profile`, `consent`, `outbox`, `submission_blocked`, `etl_run`, `migration_conflicts`) exist post-#71.
- **No code dependency on jp-adopt-forms.** The importer reads its DB schema; nothing requires the forms repo to be a Python package or sibling.

## Key technical decisions

- **Extract the intake core logic into callable helpers.** `_process_adoption` and `_process_facilitation` in `apps/api/src/jp_adopt_api/routers/intake.py` currently return `JSONResponse` — HTTP-shaped, awkward for batch use. Lift the body into `process_adoption_payload()` / `process_facilitation_payload()` that return a plain result object (`{contact_id, created, interest_ids, was_blocked}`). The HTTP endpoints become thin wrappers that JSON-encode the result. The importer calls the helpers directly. Live behavior unchanged; ergonomics for batch use much better.
- **Reuse `apps/etl/` package, don't fork.** The DT importer already built the infrastructure: `EtlRun` audit, `MigrationConflict` per-row error capture, `outbox_suppressed()` integration, dry-run, watermark, `--verbose` logging. The forms importer is a new CLI entry + new source reader + new mapper inside the same package. The dt-etl CLI is unchanged.
- **Source connection is sync psycopg2.** Mirror the existing `dt_source.py` pattern — paginated cursor reads, no asyncpg. The intake helpers being called are async; the orchestrator runs the async batch inside `asyncio.run()` per the existing `dt-etl` pattern.
- **`created_at` preservation.** Add an optional `override_created_at` parameter to `_resolve_contact` (and thread it through the new payload helpers). When the importer supplies the forms-side `created_at`, it propagates onto the `Contact` row. When the live endpoint calls the helper without it, `created_at` defaults to `now()` as today. No backfill UPDATE needed — the value lands at insert time.
- **Outbox suppression captures a single bulk event.** Per `outbox_suppression.py`, the wrapped run records each suppressed event_type and emits one `jp.adopt.v1.bulk_imported` outbox row when the context closes. The daily digest and drip worker see one event, not N. Mirrors the dt-etl convention.
- **Source-system identifier.** Use `source_system='jp-adopt-forms'` and `source_id=<forms submissions.id::text>` so the existing partial unique index handles dedup. ON CONFLICT DO NOTHING is automatic via the helpers (`_resolve_contact` already looks up by email_normalized first — which catches cross-form dedup — and then writes with these fields populated).
- **Dedup across Form A and Form B.** Handled organically by `_resolve_contact`'s email-keyed lookup. If two forms submissions share an email, the second call finds the existing contact and adds a new `AdopterInterest` row rather than a duplicate `Contact`. No new dedup code.
- **Failure handling.** Per-row errors (mapping failures, schema mismatches) write to `migration_conflicts` and continue — same pattern as `dt-etl`. The import completes with an error count; operator reviews the table.

## Implementation units

### U1. Extract intake core logic from HTTP wrappers

**Files:**
- Modify: `apps/api/src/jp_adopt_api/routers/intake.py`
- Modify: `apps/api/tests/test_intake_*.py` (existing tests stay green — refactor only)

**Goal:** Lift `_process_adoption` and `_process_facilitation` bodies into `process_adoption_payload()` / `process_facilitation_payload()` that return a plain result dataclass (`IntakeOutcome { contact_id, created, interest_ids, was_blocked, submission_id }`). The existing HTTP endpoint functions become thin wrappers that call the helper and JSON-encode the result with `_success_response` / `_error_response`. Add an optional `override_created_at: datetime | None = None` parameter on `_resolve_contact` that propagates to the new `Contact` row when supplied.

**Requirements:** Enables U4 to drive intake processing without HTTP shell.

**Dependencies:** none.

**Execution note:** Refactor with no behavior change for live endpoints. Run the existing intake test suite before and after to confirm parity.

**Approach:**
- Define `IntakeOutcome` as a small dataclass in the same module.
- New helpers take the same `(session, *, payload, settings)` shape, no `request_id` (the helper doesn't need it — callers add request-scoped fields to their own wrappers).
- The HTTP endpoint wrappers generate `request_id` and convert `IntakeOutcome` to `JSONResponse` via the existing `_success_response` / `_error_response` helpers.
- `override_created_at` defaults `None`; only the importer passes it. `_resolve_contact` sets `contact.created_at = override_created_at` only on the insert branch (existing-contact path leaves the row's `created_at` untouched).

**Patterns to follow:**
- Existing `_process_adoption` / `_process_facilitation` bodies — the lift is mechanical.
- `_resolve_contact` insert path (it already constructs the `Contact` instance — just thread the override through).

**Test scenarios:**
- **Refactor parity** — every existing `apps/api/tests/test_intake_*.py` test still passes without modification (the HTTP endpoints' observable behavior is unchanged).
- **Helper happy path (adoption)** — calling `process_adoption_payload` with a valid `AdoptionIntake` returns an `IntakeOutcome` with a real `contact_id`, `was_blocked=False`, populated `interest_ids`. The `Contact` row exists in the session.
- **Helper happy path (facilitation)** — same for `process_facilitation_payload`.
- **Helper blocked path** — calling with an email tied to a `do_not_engage` contact returns `was_blocked=True` and writes a `SubmissionBlocked` row. Matches the HTTP path's anti-enumeration behavior.
- **`override_created_at` preserved on insert** — calling with `override_created_at=<2024 timestamp>` and a fresh email produces a `Contact` with `created_at=<2024>`.
- **`override_created_at` ignored on existing contact** — calling with `override_created_at=<2024>` and an email already in the DB leaves the existing contact's `created_at` untouched.

**Verification:** All existing intake tests pass. New helper tests pass. The HTTP endpoint diff is roughly "call helper, encode result" — no logic re-implementation.

---

### U2. Forms-Postgres source reader

**Files:**
- Create: `apps/etl/src/jp_adopt_etl/forms_source.py`
- Create: `apps/etl/tests/test_forms_source.py` (fixture data + paginated iteration)

**Goal:** A psycopg2-based source reader that iterates `submissions` rows from jp-adopt-forms' Postgres, paginated, with watermark support. Yields one row at a time as `dict[str, Any]`.

**Requirements:** Source-of-truth read path for U4.

**Dependencies:** none.

**Approach:**
- Mirror `apps/etl/src/jp_adopt_etl/dt_source.py` structure: connection helper, `iter_submissions(connection, *, batch_size, since: datetime | None)` generator.
- Use a server-side cursor (`cursor('name')` in psycopg2) so memory stays bounded for large result sets.
- Project only the columns the importer needs: `id`, `form_type` (Form A vs Form B), `payload` (JSONB), `created_at`, `updated_at`. Skip `submission_audit` unless U3 reveals a need.
- Order by `created_at ASC` for stable iteration and watermark correctness.

**Patterns to follow:** `dt_source.py` — paginated cursor, batch_size param, watermark filter.

**Test scenarios:**
- **Empty result** — no rows in fixture → generator yields nothing, no error.
- **Paginated read** — fixture with 250 rows, batch_size=100 → all 250 yielded in order.
- **Watermark filter** — `since=<midpoint>` → only rows with `created_at > midpoint` yielded.
- **Connection failure** — invalid DSN → raises `OperationalError` with a clear message.

**Verification:** Unit tests pass against a fixture Postgres (or via test container; reuse the existing api test harness pattern).

---

### U3. Submission → intake payload mapper

**Files:**
- Create: `apps/etl/src/jp_adopt_etl/mappers/forms.py`
- Create: `apps/etl/tests/test_forms_mapper.py`

**Goal:** Translate one forms `submissions` row (form_type + JSONB payload + created_at) into an `AdoptionIntake` or `FacilitationIntake` Pydantic instance, ready for U1's helpers. Capture mapping failures in a structured result so U4 can route them to `migration_conflicts`.

**Requirements:** Bridges forms' wire format to adopt-core's intake schemas.

**Dependencies:** depends on U1 (consumes the unchanged `AdoptionIntake`/`FacilitationIntake` schemas), but no code dependency — schemas exist today.

**Approach:**
- Discriminate on `submissions.form_type` (or whatever the actual column is — discovered at implementation, see Deferred to Implementation).
- For each form type, build the corresponding Pydantic payload from the JSONB. Pydantic raises `ValidationError` on bad input.
- Return a `MapResult` union: `Success(payload, source_id, created_at)` or `Failure(reason, source_id, source_payload)`.
- `source_id` = `str(submissions.id)`; the importer threads this through to the `Contact.source_id` field via the helper.
- `created_at` from the submissions row, threaded through as `override_created_at`.

**Technical design:** the mapper is a pure function (no I/O). Inputs are dict + form_type; output is `MapResult`. This keeps it trivially unit-testable with fixture JSONBs.

**Patterns to follow:**
- `apps/etl/src/jp_adopt_etl/mappers/contacts.py` — pure-function shape, structured return on failure.
- The existing `AdoptionIntake` / `FacilitationIntake` field validators in `apps/api/src/jp_adopt_api/schemas.py` — these enforce the contract.

**Test scenarios:**
- **Happy path adoption** — valid Form A JSONB → `Success` with a populated `AdoptionIntake` (display_name, email, country_code, fpg_selections, profile, consents).
- **Happy path facilitation** — valid Form B JSONB → `Success` with a populated `FacilitationIntake` (same shape + facilitation-specific fields).
- **Missing required field** — JSONB without `email` → `Failure("validation_error: missing email", source_id, payload)`.
- **Unknown form_type** — `form_type="unknown"` → `Failure("unknown_form_type", source_id, payload)`.
- **Bad enum** — JSONB with an unrecognized `commitment_level` → `Failure("validation_error: <pydantic error>", …)`.
- **`created_at` returned** — `MapResult.created_at` matches the submissions row's `created_at`.

**Verification:** Mapper covers both form types; failure shapes match what U4 can serialize into `migration_conflicts.source_value`.

---

### U4. Forms importer orchestrator + CLI

**Files:**
- Create: `apps/etl/src/jp_adopt_etl/forms_orchestrator.py`
- Modify: `apps/etl/pyproject.toml` (add `forms-etl` entry point)
- Create: `apps/etl/tests/test_forms_orchestrator.py`

**Goal:** End-to-end batch driver. Reads from U2, maps via U3, calls U1 helpers inside `outbox_suppressed()`, writes `EtlRun` audit, writes per-row `migration_conflicts` for failures. Supports dry-run, watermark, and verbose logging. CLI entry `forms-etl`.

**Requirements:** Closes the issue's acceptance criteria (match queue populated, contacts list populated, idempotent re-run, no drip emails during import).

**Dependencies:** U1, U2, U3.

**Approach:**
- CLI shape mirrors `dt-etl`: `--forms-postgres-url`, `--postgres-url` (target), `--dry-run`, `--watermark`, `--batch-size`, `--verbose`.
- Per-table loop: only one table (`submissions`) here, but keep the `EtlRun` row per "table name" so the audit dashboard is uniform with dt-etl.
- For each row: map via U3. On `Failure`, write `MigrationConflict(source_system='jp-adopt-forms', source_id, table_name='submissions', conflict_type=<reason>, source_value=<payload>)` and continue. On `Success`, call the appropriate U1 helper inside `outbox_suppressed()`. The helper's `IntakeOutcome` tells us whether the contact was blocked (`was_blocked=True` is logged but not a failure — anti-enumeration behavior is preserved).
- Dry-run: open `outbox_suppressed()` and a savepoint; map every row, attempt helper calls, then rollback. Writes the `EtlRun` row with `mode='dry_run'` and counters.
- Production: same flow without rollback; commits per batch (configurable batch size for transactional grouping).
- Watermark: `--watermark <ISO timestamp>` filters the source reader; on success, writes the max `created_at` seen to `EtlRun.source_max_modified_at` for the next run to pick up.
- Logging: per-row outcome (`imported`, `skipped_conflict`, `blocked`, `mapping_failed`) at INFO when `--verbose`, else summary at the end.

**Patterns to follow:**
- `apps/etl/src/jp_adopt_etl/orchestrator.py` — the dt-etl orchestrator is the canonical template for everything (argparse shape, `EtlRun` lifecycle, `outbox_suppressed` wrapping, `asyncio.run` + sync source).
- `apps/etl/runbook.md` — the dt-etl operator usage is the shape U5 will mirror.

**Test scenarios:**
- **End-to-end happy path (dry_run)** — 3 valid submissions in fixture forms DB → orchestrator maps all 3, calls helpers under a rolled-back transaction, writes one `etl_run` row with `imported_count=3`, no contacts persisted in target DB, no outbox events.
- **End-to-end happy path (production)** — same 3 → 3 contacts persisted, 3 `adopter_interest` rows, 1 `outbox` row with `event_type='jp.adopt.v1.bulk_imported'`.
- **Idempotent re-run** — run twice in succession → second run finds 0 new submissions (watermark) OR if no watermark, finds same 3 but `ON CONFLICT DO NOTHING` (via the helper's `_resolve_contact` email-lookup) creates no new contacts and no new interest duplicates beyond what U1 already handles.
- **Mapping failure routed to migration_conflicts** — fixture row with a missing email → `migration_conflict` row written with `conflict_type='validation_error: missing email'`; orchestrator continues; final summary shows `failed_count=1`.
- **Outbox suppression captures bulk event** — production run with N rows → exactly 1 outbox row of type `jp.adopt.v1.bulk_imported` (per `outbox_suppressed` contract).
- **Blocked contact handled gracefully** — fixture row whose email matches a seeded `do_not_engage` contact → `IntakeOutcome(was_blocked=True)`; counted as `blocked_count`, not `failed_count`; no `SubmissionBlocked` write conflict.
- **`created_at` preservation** — fixture row with `created_at='2024-11-15T10:00:00Z'` → resulting `Contact.created_at` equals that timestamp.
- **Watermark advances** — production run finds max `created_at` of `2024-12-01`; the resulting `etl_run.source_max_modified_at` row records that value.

**Verification:**
- All scenarios above pass.
- `uv run --package jp-adopt-etl forms-etl --dry-run …` against a real forms-DB snapshot completes with a per-row outcome summary and creates exactly one `etl_run(mode='dry_run')` row in the target.
- Production run against the same snapshot persists the expected count of contacts (verified by SQL count vs source count) and emits one bulk outbox event.

---

### U5. Operator runbook

**Files:**
- Create: `docs/runbooks/forms-data-import.md`

**Goal:** Single document an operator (Joel, on-call) can follow to dry-run + cutover-import forms data into production, plus re-run procedures for subsequent imports.

**Requirements:** Issue acceptance: "Procedure is documented for re-runnability."

**Dependencies:** U4 (the CLI exists).

**Approach:** Mirror `docs/runbooks/dt-cutover.md` shape, scoped to forms:
- **Preflight**: network access check (laptop+Tailscale or ACA exec — both paths documented); credential retrieval from Key Vault or one-shot; target DB migration version check.
- **Dry-run**: command, expected output, what to inspect (`etl_run` row, `migration_conflicts` count, sample of would-be contacts).
- **Production import**: command, expected output, verification queries (row counts vs source, sample 5 contacts spot-check, outbox bulk event present, no per-row drip events).
- **Re-run / incremental**: how to read the prior `etl_run.source_max_modified_at` and pass as `--watermark`.
- **Rollback**: how to identify and delete imported rows by `source_system='jp-adopt-forms'` if the import landed bad data.
- **Troubleshooting**: `migration_conflicts` triage steps, common `conflict_type` values and remediation.

**Test scenarios:** Test expectation: none — documentation. Verified by an end-to-end runbook walkthrough against the staging or production stack.

**Verification:** Runbook is self-contained — an operator who has not built the importer can run it successfully from the runbook alone.

---

## System-wide impact

- **API surface:** no public API change. U1 is a refactor with identical HTTP behavior.
- **Database:** no schema change. Uses existing `etl_run`, `migration_conflicts`, and the existing `contacts` partial unique index for idempotency.
- **Outbox:** one new event_type observed in production logs (`jp.adopt.v1.bulk_imported`) when the importer runs. Worker drains it as unhandled (logged but not actioned), matching the dt-etl pattern.
- **Worker / drip / digest:** no new events fire per imported row. Outbox suppression collapses N events into 1 bulk event per run.
- **Operations:** new operator capability — re-runnable forms import. Documented in U5.

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Forms `submissions` schema differs from this plan's assumptions (column names, JSONB shape, form_type discrimination). | High — schema lives in a separate repo not read here. | U3's mapper is the single source of schema knowledge. Discover the real shape during U3 implementation by reading a sample row; adjust the mapper and document the assumed shape in code comments. |
| `_process_adoption` refactor introduces a subtle behavior change that the existing tests don't catch. | Low — the test suite for intake is thorough (per #71 work). | U1's execution note: run the full intake test suite before and after. If anything fails, the refactor is not yet correct. |
| `created_at` override on `_resolve_contact` opens a foot-gun where live endpoints accidentally start passing a non-None value. | Low — parameter is optional, defaults None; only the importer passes it. | Document the parameter as "import-only" in the docstring. Add a test that asserts the live POST endpoints never set it. |
| Importer runs in an environment that can reach forms' Postgres but not adopt-core's (or vice versa). | Medium — cross-env networking is fiddly. | U5 runbook's preflight section verifies both connections before any work. |
| Production run hits a forms row whose JSONB has a never-seen-before shape and crashes the orchestrator (not just one row). | Low — U3 catches `ValidationError` per row and routes to `migration_conflicts`. | Defense-in-depth: wrap the per-row helper call in a try/except that converts any unhandled exception to a `migration_conflict` rather than killing the run. |
| Duplicate adopters across Form A + Form B create unwanted `adopter_interest` rows when the same email submits both. | Medium — by design, multiple interests per contact is valid. | Acceptance: that's actually correct behavior — one contact, multiple expressed interests is the intake model. Verify in U4's tests that the resulting state is what we want. |
| Outbox suppression bug causes per-row events to leak. | Low — `outbox_suppressed()` has been validated by dt-etl in May cutover. | U4 test scenario: production run with N rows → assert exactly 1 outbox row, type `jp.adopt.v1.bulk_imported`. |
| Forms-side credentials live somewhere non-obvious; operator can't find them on import day. | Medium — first time we're connecting to forms' DB from this repo. | U5 runbook §Preflight enumerates credential sources (1Password vault path, Key Vault secret name) explicitly. |

## Open questions deferred to implementation

- **Exact forms `submissions` table column names and JSONB shape.** This plan assumes `id`, `form_type`, `payload`, `created_at`, `updated_at`. U3's mapper is where this gets pinned down — read a real row first, then code the mapper. If the columns differ, adjust the mapper and U2's projection list; nothing downstream needs to change.
- **Discrimination signal for Form A vs Form B.** Could be `form_type` enum, could be presence of a key in the JSONB (e.g., `is_facilitator: true`). Implementer picks based on what's actually in the table.
- **`form_type` values themselves.** The plan assumes "Form A = adoption, Form B = facilitation"; the actual strings in the column (`"adoption"`, `"facilitation"`, `"a"`, `"b"`, etc.) need to be confirmed at U3 time.
- **Whether `submission_audit` is needed.** Plan assumes only `submissions` is read. If the real `created_at` lives in `submission_audit` (e.g., as the first `state='received'` event), U2's projection grows to include that table.
- **CLI entry point name in `pyproject.toml`.** Plan suggests `forms-etl`. Implementer can confirm it doesn't collide with anything in the existing `apps/etl/pyproject.toml`.
- **Batch commit size.** Default 100 rows per transaction is a reasonable starting point; tune if forms has a much smaller or much larger row count than DT's ~few-thousand.

## Verification (whole-plan)

- **Refactor parity:** API test suite green before and after U1; observable behavior of the live `/v1/intake/*` endpoints unchanged.
- **Importer correctness:** U2/U3/U4 unit tests pass. CLI integration test (or runbook walkthrough) shows dry-run → production import → re-run with watermark completes cleanly against a real forms-DB snapshot.
- **Production smoke:** after the operator runs `forms-etl` against production once:
  - `SELECT COUNT(*) FROM contacts WHERE source_system='jp-adopt-forms'` matches the count of valid forms submissions.
  - `SELECT * FROM outbox WHERE event_type='jp.adopt.v1.bulk_imported' ORDER BY created_at DESC LIMIT 1` has exactly one new row.
  - No new rows in `outbox` of per-row submission event types (`jp.adopt.v1.submission_received`) from this run.
  - Match queue (`/matches`) and contacts list (`/contacts`) in the live web UI render historical records, sorted appropriately.
  - `SELECT source_system, source_id, COUNT(*) FROM contacts GROUP BY 1,2 HAVING COUNT(*) > 1` returns no duplicate rows.
- **Re-runnability:** running `forms-etl` a second time (without `--watermark`) creates no new contacts or interest duplicates beyond what U1's helpers handle; with `--watermark`, only new submissions are processed.
