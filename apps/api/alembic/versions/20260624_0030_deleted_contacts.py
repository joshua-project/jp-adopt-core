"""Create deleted_contacts suppression table

Revision ID: 0030
Revises: 0029
Create Date: 2026-06-24

Records core-side hard-deletes (``DELETE /v1/contacts/{id}``) so the hourly
DT contacts ETL won't silently re-import a contact Amy permanently removed.

The inverse of ``etl_deleted_in_source`` (which records source-side
deletions for review): this table records *our* deletions so the importer
skips them. Keyed on ``(source_system, source_id)`` — the contacts
idempotency convention — with an ``email_normalized`` fallback for
forms-sourced contacts that have no source_id.

Interim bridge until DT is decommissioned.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Alembic ID metadata
revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deleted_contacts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("source_system", sa.Text(), nullable=True),
        sa.Column("source_id", sa.Text(), nullable=True),
        sa.Column("email_normalized", sa.Text(), nullable=True),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_by", sa.Text(), nullable=True),
    )
    # Partial unique index mirroring ``uq_contacts_source_system_source_id``
    # so the ETL suppression lookup has a stable ON CONFLICT target and
    # re-deletes don't duplicate rows.
    op.create_index(
        "uq_deleted_contacts_source",
        "deleted_contacts",
        ["source_system", "source_id"],
        unique=True,
        postgresql_where=sa.text("source_id IS NOT NULL"),
    )
    op.create_index(
        "ix_deleted_contacts_email_normalized",
        "deleted_contacts",
        ["email_normalized"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_deleted_contacts_email_normalized", table_name="deleted_contacts"
    )
    op.drop_index("uq_deleted_contacts_source", table_name="deleted_contacts")
    op.drop_table("deleted_contacts")
