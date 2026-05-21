---
title: Never edit an Alembic migration file after it has been applied
date: 2026-05-20
category: conventions
module: database/migrations
problem_type: convention
component: database
severity: medium
applies_when:
  - You are about to edit an Alembic migration file that has already been applied via `alembic upgrade head`
  - A schema change was omitted from a migration and you are tempted to add it to the existing file instead of creating a new one
  - A dev Postgres instance has the old schema and the `alembic_version` row already records the latest revision
related_components:
  - development_workflow
tags:
  - alembic
  - migrations
  - database
  - postgres
  - convention
---

# Never edit an Alembic migration file after it has been applied

## Context

During U8 and U10 implementation in `jp-adopt-core`, the same process anti-pattern surfaced twice: a migration file was edited after `alembic upgrade head` had already applied it to the dev database. In U8, the `facilitator_role` migration needed a partial unique index on `contacts(source_system, source_id)` added mid-implementation for `ON CONFLICT` support. In U10, the `drip_engine` migration needed an `outbox.drip_processed_at` column added after the ARQ enrollment drain's requirements emerged. Both times, the inline edit desynced the upgrade/downgrade pair from the actual DB state, broke `alembic downgrade`, and required manual SQL workarounds to recover.

## Guidance

**Never edit a migration file after `alembic upgrade head` has run against any environment (including your local dev DB). Create a new migration instead.**

Wrong — editing an already-applied migration:

```bash
# Migration 0010 already ran. You need drip_processed_at on outbox.
# DON'T do this:
vim apps/api/alembic/versions/20260520_0010_drip_engine.py
# ... add op.add_column() to upgrade(), op.drop_column() to downgrade()
alembic downgrade 0009 && alembic upgrade head
# Downgrade FAILS — column never existed in the original 0010 that was applied
```

Right — create a new migration for the new change:

```bash
# Generate the next revision (substitute your project's actual next number;
# in this repo, after 0011 it would be 0012)
alembic revision -m "drip_processed_at_on_outbox"
# File: 20260520_0012_drip_processed_at_on_outbox.py
#
# upgrade():
#     op.add_column('outbox',
#         sa.Column('drip_processed_at', sa.DateTime(timezone=True), nullable=True))
# downgrade():
#     op.drop_column('outbox', 'drip_processed_at')

alembic upgrade head
# Clean apply; upgrade/downgrade pair is invertible
```

The new file is the right granularity: small, single-purpose, and invertible. Fresh DB clones replay all migrations in order; environments that already ran the prior revision apply only the new delta.

**Escape hatch — use with caution.** If you are absolutely certain the migration has only run on a single local dev DB that you control, and that DB has not been snapshotted or shared, you can downgrade to the prior revision *first*, then edit the file, then upgrade:

```bash
alembic downgrade 0009   # must succeed before the edit
# edit 0010 now
alembic upgrade head
```

This only works if the downgrade executes cleanly against the unmodified state. The moment you edit the file before downgrading, the downgrade script no longer matches what the DB contains. If anyone else has pulled the branch and run the migration, you're back to needing a new revision.

**Prevention.** Add a pre-commit hook that warns when a previously-committed migration is being modified:

```bash
#!/usr/bin/env bash
# .git/hooks/pre-commit (or wired through a hook manager)
BASE=$(git merge-base origin/main HEAD)
CHANGED=$(git diff --name-only "$BASE" HEAD -- 'apps/api/alembic/versions/' | xargs -I{} sh -c '
  # Flag files that existed at $BASE but have been modified since
  if git show "$BASE:{}" >/dev/null 2>&1; then echo "{}"; fi
')
if [ -n "$CHANGED" ]; then
  echo "WARNING: previously-committed migration file(s) modified:"
  echo "$CHANGED"
  echo "Create a new migration with 'alembic revision -m <name>' instead."
  exit 1
fi
```

## Why This Matters

Alembic's migration files are a versioned, ordered ledger — each file is a contract that says "if you ran `upgrade N`, running `downgrade N` returns you exactly to `N-1`." Editing a migration after it has applied breaks that contract for every environment where it already ran. `IF NOT EXISTS` / `IF EXISTS` guards suppress the immediate error symptom but do **not** restore the upgrade/downgrade inverse property: the downgrade still tries to undo something that was never done, and the upgrade no longer represents what that revision actually introduced in older environments. In CI, staging, or a teammate's dev DB, the broken downgrade will surface as a hard failure or — worse — silent schema drift that diverges from what `alembic current` claims.

## When to Apply

- Any time you need to add, remove, or change schema objects (columns, indexes, constraints, tables) in a migration that has already been applied to **any** environment — local, CI, staging, or production.
- Any time `alembic current` reports a revision as `head` and you are tempted to open that revision's `.py` file to extend it.
- Does **not** apply to migrations that exist only as uncommitted files that have never been run — those are safe to edit freely before first use.

## Examples

**Before — broken pattern (U10 session):**

- `20260520_0010_drip_engine.py` had been applied to the dev DB.
- ARQ enrollment drain needed a new `drip_processed_at` column on `outbox`.
- The migration file was edited in place to add the column to `upgrade()` and a matching `drop_column` to `downgrade()`.
- `alembic downgrade 0009` failed with `column "drip_processed_at" of relation "outbox" does not exist` — the column the downgrade was trying to drop had never been created.
- Workaround: an inline asyncpg script ran `ALTER TABLE outbox ADD COLUMN IF NOT EXISTS drip_processed_at TIMESTAMPTZ NULL` to backfill the dev DB. CI worked because CI starts from a fresh DB and replays migrations from `base`. Other developers' dev DBs were silently divergent until they wiped and re-cloned.

**After — correct pattern:**

- Leave `20260520_0010_drip_engine.py` as-applied.
- Create the next revision file (e.g. `20260520_0012_add_drip_processed_at.py`) with one `op.add_column` in `upgrade()` and one `op.drop_column` in `downgrade()`.
- `alembic upgrade head` applies cleanly.
- `alembic downgrade <prior>` removes only that column — no divergence, no manual SQL, no broken CI, no surprise for teammates pulling the branch.

## Related

- `dt-adoption-platform/docs/solutions/database-issues/prisma-migrate-fails-after-jpadmin-to-per-app-user-pivot.md` — different ORM (Prisma) and different root cause (per-app DB user pivot), but same domain: migration tooling fails silently when the file-system view diverges from the DB's actual state. Cross-ecosystem context, not the same problem.
