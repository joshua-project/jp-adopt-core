"""Add partial unique index for migration_conflicts dedup

Revision ID: 0025
Revises: 0024
Create Date: 2026-06-04

The DT ETL writes ``migration_conflicts`` rows on every run (e.g. once per
``local_assignment_override`` per overridden contact). With an hourly cron
this accumulates linearly — 720 duplicate rows per contact per month for a
recurring conflict. A partial unique index on
``(source_system, source_id, table_name, conflict_type)`` lets the ETL use
``INSERT … ON CONFLICT DO NOTHING`` so each (run-output) conflict is recorded
at most once. ``source_system`` is NOT NULL in the existing schema, so the
partial predicate only excludes locally-created conflicts that may carry
NULL source_id (none exist today, but the partial guard is consistent with
the contacts/activity_log/staff_identity_link pattern).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop any pre-existing duplicate rows so the unique index can be created.
    # Keep the earliest detected_at per natural key.
    op.execute(
        """
        DELETE FROM migration_conflicts a
        USING migration_conflicts b
        WHERE a.id <> b.id
          AND a.source_system = b.source_system
          AND a.source_id = b.source_id
          AND a.table_name = b.table_name
          AND a.conflict_type = b.conflict_type
          AND a.detected_at > b.detected_at
        """
    )
    op.create_index(
        "uq_migration_conflicts_natural_key",
        "migration_conflicts",
        ["source_system", "source_id", "table_name", "conflict_type"],
        unique=True,
        postgresql_where=sa.text("source_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_migration_conflicts_natural_key",
        table_name="migration_conflicts",
    )
