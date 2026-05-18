"""magic-link side-car tables (U3)

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-15

Adds:
  * magic_link_token (single-use, 15-min TTL, claim audit columns)
  * magic_link_rate_limit (per-email request counter, used to enforce
    6/hour/email throttle in the request endpoint)
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "magic_link_token",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("email_normalized", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_ip", sa.Text(), nullable=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_ip", sa.Text(), nullable=True),
        sa.Column("claimed_user_agent", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_magic_link_token_email_normalized",
        "magic_link_token",
        ["email_normalized"],
    )
    op.create_index(
        "ix_magic_link_token_expires_at",
        "magic_link_token",
        ["expires_at"],
    )

    op.create_table(
        "magic_link_rate_limit",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email_normalized", sa.Text(), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_magic_link_rate_limit_email_requested",
        "magic_link_rate_limit",
        ["email_normalized", "requested_at"],
    )

    # Own to migrator role when present (per-app DB user discipline).
    op.execute(
        """
        DO $$
        DECLARE
            t text;
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jp_adopt_migrator') THEN
                FOR t IN
                    SELECT unnest(ARRAY[
                        'magic_link_token',
                        'magic_link_rate_limit'
                    ])
                LOOP
                    EXECUTE format('ALTER TABLE %I OWNER TO jp_adopt_migrator', t);
                END LOOP;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_magic_link_rate_limit_email_requested",
        table_name="magic_link_rate_limit",
    )
    op.drop_table("magic_link_rate_limit")
    op.drop_index(
        "ix_magic_link_token_expires_at",
        table_name="magic_link_token",
    )
    op.drop_index(
        "ix_magic_link_token_email_normalized",
        table_name="magic_link_token",
    )
    op.drop_table("magic_link_token")
