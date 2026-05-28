"""contact_assignment: per-contact staff owner (DT parity, Group C / U13)

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-26

DT has a single ``assigned_to`` per contact; jp-adopt-core routes to facilitator
*orgs* and never modeled a staff owner. This adds a 1:1 ``contact_assignment``
(contact_id is the PK, so one assignee per contact; re-assigning replaces).
Kept off the ``contacts`` row so assigning doesn't bump ``Contact.version``
(the optimistic-lock column the match/transition flows gate on). The subject
column is ``user_subject_id`` to match the post-Entra naming (migration 0012).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "contact_assignment",
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("user_subject_id", sa.Text(), nullable=False),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("assigned_by", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_contact_assignment_user",
        "contact_assignment",
        ["user_subject_id"],
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jp_adopt_migrator') THEN
                EXECUTE 'ALTER TABLE contact_assignment OWNER TO jp_adopt_migrator';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_contact_assignment_user", table_name="contact_assignment")
    op.drop_table("contact_assignment")
