"""Create duplicate_review_decision table

Revision ID: 0033
Revises: 0032
Create Date: 2026-06-25

Backs the staff-admin "Review duplicates" UI. Each ``duplicate_email``
migration_conflict is a DT-origin contact whose email collides with an
existing (usually forms-intake) contact; the importer NULLs the colliding
email and records the conflict. Track A auto-merges only the high-confidence
name+email matches — the ambiguous remainder sits as conflict rows for a human
to judge.

This table records that human judgement, keyed on
``(email_normalized, dt_source_id)`` (the conflict's identity):

  * ``merge``  — reviewer confirms the DT record IS the email owner. The next
    hourly Track A run reads these as ``force_merge`` / ``multi_keep`` decisions
    and applies the DT-authoritative merge (after which the conflict row is
    deleted, so it drops off the review list).
  * ``ignore`` — reviewer confirms they are DIFFERENT people sharing an inbox
    (e.g. ``upgadoption@aims.org``, a church address). The conflict is filtered
    out of the review list; Track A already declines to merge it.

Interim bridge until DT is decommissioned.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Alembic ID metadata
revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "duplicate_review_decision",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email_normalized", sa.Text(), nullable=False),
        sa.Column("dt_source_id", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('merge', 'ignore')",
            name="ck_duplicate_review_decision_decision",
        ),
        sa.UniqueConstraint(
            "email_normalized",
            "dt_source_id",
            name="uq_duplicate_review_decision_conflict",
        ),
    )
    # Track A loads all 'merge' rows each run; index the lookup column.
    op.create_index(
        "ix_duplicate_review_decision_decision",
        "duplicate_review_decision",
        ["decision"],
    )


def downgrade() -> None:
    op.drop_index("ix_duplicate_review_decision_decision")
    op.drop_table("duplicate_review_decision")
