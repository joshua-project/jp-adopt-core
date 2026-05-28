"""consent: MOU acceptance records (DT parity, Group B / U8)

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-26

The adoption/facilitation forms capture an MOU acceptance as a consent record
(see jp-adopt-forms ``consentRecordApiSchema``): a content-addressed
acknowledgement with the accepted version, a sha-256 ``content_hash`` of the
shown text, when it was accepted, and optional conversational ``evidence``.
Modeled as its own table (1:N per contact) rather than a status flag so the
acceptance is auditable.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "consent",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("consent_type", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_consent_content_hash_sha256",
        ),
    )
    op.create_index(
        "ix_consent_contact_type",
        "consent",
        ["contact_id", "consent_type"],
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jp_adopt_migrator') THEN
                EXECUTE 'ALTER TABLE consent OWNER TO jp_adopt_migrator';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_consent_contact_type", table_name="consent")
    op.drop_table("consent")
