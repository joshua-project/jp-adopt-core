---
title: Batch importer over async intake helpers — savepoint, dedup set, batch commit
date: 2026-05-29
module: apps/etl
problem_type: architecture_pattern
component: background_job
severity: medium
applies_when:
  - "Building a batch importer that reuses async HTTP-shaped intake handlers in-process"
  - "Source-system rows have a stable id you can key idempotency on via a partial unique index (`source_system`, `source_id`)"
  - "The intake handlers touch multiple tables and you can't tolerate per-row failures killing the whole run"
  - "Imports may span thousands of rows where one giant transaction would block autovacuum or lose work on crash"
related_components:
  - database
  - tooling
tags:
  - etl
  - batch-import
  - sqlalchemy
  - savepoint
  - transactional-outbox
  - idempotency
---

## Context

`apps/etl/dt-etl` (DT → adopt-core) and `apps/etl/forms-etl` (jp-adopt-forms → adopt-core) both drain a source system into adopt-core by reusing the canonical intake processing logic (`process_adoption_payload` / `process_facilitation_payload`) instead of reimplementing it. The intake helpers are async, write multiple tables (`contacts`, `adopter_interest`, `contact_profile`, `consent`, `outbox`), and were designed for live HTTP traffic — one request, one transaction, fail loud.

Driving them from a batch loop changes the failure model. A single per-row exception can poison the SQLAlchemy session for every subsequent row; an all-at-end commit loses every prior row's work on crash; and a naive per-row dedup check costs N round trips. The pattern below is what survived two PR review cycles on PR #85.

## Guidance

Three techniques compose into a robust batch importer. None of them work without the other two.

### 1. SAVEPOINT per row via `session.begin_nested()`

Wrap each per-row helper call in `async with session.begin_nested()`. SQLAlchemy turns this into a Postgres `SAVEPOINT`. Any exception inside the block — including the realistic case of `sqlalchemy.exc.IntegrityError` from a concurrent insert or FK race — rolls back ONLY that row's writes. The outer transaction stays alive, and your follow-up `_record_conflict(session, ...)` call writes its row against a clean session instead of inheriting `PendingRollbackError` state.

```python
async def _import_one(session, *, mapped, settings) -> str:
    try:
        async with session.begin_nested():
            outcome = await process_adoption_payload(
                session,
                payload=mapped.payload,
                settings=settings,
                override_created_at=mapped.created_at,
                source_system=SOURCE_SYSTEM,
                source_id=mapped.source_id,
            )
    except IntakeValidationError as exc:
        # SAVEPOINT auto-rolled back by the context manager. Session is clean.
        await _record_conflict(session, source_id=mapped.source_id, ...)
        return "skipped_conflict"
    except Exception as exc:  # noqa: BLE001 — per-row defense in depth
        await _record_conflict(session, ...)
        return "skipped_conflict"
    return "blocked" if outcome.was_blocked else "imported"
```

Without the savepoint, the `except Exception` branch can fire AFTER the session is already poisoned, and `_record_conflict`'s `session.add()` then raises `PendingRollbackError` — the whole import dies instead of gracefully recording the conflict.

### 2. Pre-fetched dedup set, not per-row SELECT

Idempotency via the `(source_system, source_id)` partial unique index is correct, but DON'T check it per row with a separate SELECT — that's N round trips at run start. Pre-fetch every imported `source_id` into a set once, check against the set in memory, and add to the set as the run progresses (so duplicate `source_id`s within the same batch — rare but possible with concurrent writes mid-import — also dedupe correctly):

```python
async def _load_imported_source_ids(session) -> set[str]:
    rows = await session.execute(
        select(Contact.source_id).where(Contact.source_system == SOURCE_SYSTEM)
    )
    return {sid for sid in rows.scalars().all() if sid is not None}

# In the per-row loop:
seen_imported = set(imported_source_ids)  # from pre-fetch
for row in rows:
    ...
    if result.source_id in seen_imported:
        continue  # already imported in a prior run
    label = await _import_one(session, mapped=result, settings=settings)
    if label == "imported":
        seen_imported.add(result.source_id)
```

The savepoint from technique #1 is the backstop: if a row sneaks past the set check (concurrent importer, stale set), the `IntegrityError` from the partial unique index surfaces as a `processing_error` in `migration_conflicts` instead of corrupting the run.

### 3. Commit every N rows in production; never in dry-run

Production runs commit every `batch_size` rows (we use 100). A crash on row 9,500 of 10,000 then keeps the first 9,500. The outer transaction stays short enough to not block autovacuum on `contacts` and `adopter_interest`. The savepoint scope is per-row, so committing the outer transaction doesn't strand any half-written row — savepoints have either released or rolled back by then.

Dry-run runs MUST NOT commit mid-loop. Dry-run uses `async with session.begin_nested()` (outer-level) and `await nested.rollback()` at the end; the whole point is leaving zero rows behind. Any mid-run commit defeats it.

```python
async def _process_rows(session, *, rows, settings, commit_every: int | None = None):
    rows_since_commit = 0
    for row in rows:
        ...
        rows_since_commit += 1
        if commit_every is not None and rows_since_commit >= commit_every:
            await session.commit()
            rows_since_commit = 0

# Caller:
if mode == "production":
    counts = await _process_rows(..., commit_every=batch_size)
else:
    # dry-run: outer begin_nested + rollback; no commit_every
    counts = await _process_rows(..., commit_every=None)
```

## Why This Matters

**Session poisoning is silent until it isn't.** A live HTTP intake call that hits an `IntegrityError` returns a 500 to the caller and dies — the caller retries, life moves on. A batch importer that hits the same error mid-row, then tries to record a `migration_conflict` against the poisoned session, dies in a more confusing way: the `_record_conflict` write fails with `PendingRollbackError`, the orchestrator's outer `try`/`except` swallows that too, and the run aborts somewhere far from the original failure with no useful error in the audit table. The savepoint pattern makes the failure mode the one you actually want: one conflict row written, run continues.

**Per-row SELECTs are fine until they aren't.** At hundreds of rows the N+1 is cosmetic. At tens of thousands it adds minutes of round-trip latency on a connection that's already inside a long-running transaction. The pre-fetch set collapses to one query at run start.

**All-at-end commits are an "I trust this run will complete" bet.** That bet is fine for the third re-run of the same script against a known-clean snapshot. It's a bad bet for the first production cutover against real data you've never seen before. Per-batch commits make crash recovery trivial — the watermark in `etl_run` plus the dedup set means the next run picks up where the last one stopped, even if the last one was a SIGKILL.

## When to Apply

- Any batch importer that reuses live request-handlers in-process (rather than re-implementing the persistence logic).
- Imports where source-row count may grow past a few hundred.
- Imports where the data shape isn't fully known until the first run.
- Migrations/backfills using `outbox_suppressed()` so downstream consumers see one bulk event per run instead of N.

Skip the per-batch commit on:

- Dry-run modes that use savepoint+rollback at the run level.
- Imports that genuinely must be all-or-nothing (transactional consistency requirement that crosses rows — rare in import scenarios).

## Examples

See PR #85 (`feat/forms-historical-import` → main) and `apps/etl/src/jp_adopt_etl/forms_orchestrator.py` for the working reference. The `dt-etl` orchestrator predates this pattern and uses an older shape (single `outbox_suppressed` wrap, no per-row savepoint) — works fine for the one-shot DT cutover but would not survive a re-runnable production loop.

## Related discipline notes (smaller lessons from the same PR cycle)

These didn't merit their own learning docs but are worth surfacing for the next agent:

1. **Refactors that lift HTTP handlers into batch-callable helpers must preserve embedded WHY-comments verbatim.** The first U1 commit in PR #85 dropped ~30 lines of comments explaining anti-enumeration N1 oracle defense (`fabricated_interest_ids`), F15 PII-light fingerprint rationale, and the body-shape oracle. Code was correct; rationale was gone. Future readers seeing `fabricated_interest_ids = [uuid.uuid4() for _ in ...]` with no context would have "simplified" it and reintroduced the `do_not_engage` oracle. AGENTS.md's "Default to writing no comments. Only add one when the WHY is non-obvious" is exactly what those comments were written for — non-obvious WHY for security-critical code. Preserve.

2. **The standard one-pass camelCase→snake_case regex is wrong for acronyms.** The shipped `re.compile(r"(?<!^)(?=[A-Z])")` + `.lower()` mapping turns `URL` → `u_r_l`, `IDField` → `i_d_field`, `HTTPResponseCode` → `h_t_t_p_response_code`. The correct two-pass form is:

    ```python
    _ACRONYM_TAIL = re.compile(r"(.)([A-Z][a-z]+)")
    _LOWER_UPPER = re.compile(r"([a-z0-9])([A-Z])")

    def _camel_to_snake(name: str) -> str:
        intermediate = _ACRONYM_TAIL.sub(r"\1_\2", name)
        return _LOWER_UPPER.sub(r"\1_\2", intermediate).lower()
    ```

    Verified outputs: `URL → url`, `URLPath → url_path`, `IDField → id_field`, `userID → user_id`. Caught by a pre-merge spot-check, not by tests (Prisma defaults to camelCase, not UPPERCASE, so existing fixtures didn't exercise acronyms).

3. **Plan deviations discovered at implementation must update the plan file, not just the PR description.** When the real forms schema turned out to be `adoption_submissions` + `facilitation_submissions` + child tables (not a single JSONB `submissions` table the plan assumed), the implementer correctly flagged it in the PR body and added a deviation note at the bottom of the plan — but didn't update the plan's `Problem frame` and `In scope` sections that still claimed the original shape. The plan then became internally inconsistent: top half says one thing, bottom half says another. Next agent picking up an adjacent task trusts the top half, builds on the wrong premise. When deviating, update the plan body to match reality. The deviation note at the bottom is good for historical record but should annotate, not replace, the up-to-date sections.
