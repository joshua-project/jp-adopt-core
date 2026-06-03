"""Add adopter_interest source keys for DT ETL idempotency

Revision ID: 0023
Revises: 0022
Create Date: 2026-06-03

The DT ETL imports per-FPG interests from ``fpg_submission_data`` and needs an
idempotent ``ON CONFLICT (source_system, source_id)`` target. Mirrors the
contacts/activity_log partial unique index from migration 0009. Locally-created
rows carry ``source_system='local'`` with NULL ``source_id`` and are excluded
from the uniqueness check.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "adopter_interest",
        sa.Column(
            "source_system",
            sa.Text(),
            nullable=False,
            server_default="local",
        ),
    )
    op.add_column(
        "adopter_interest",
        sa.Column("source_id", sa.Text(), nullable=True),
    )
    op.create_index(
        "uq_adopter_interest_source_system_source_id",
        "adopter_interest",
        ["source_system", "source_id"],
        unique=True,
        postgresql_where=sa.text("source_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_adopter_interest_source_system_source_id",
        table_name="adopter_interest",
    )
    op.drop_column("adopter_interest", "source_id")
    op.drop_column("adopter_interest", "source_system")
