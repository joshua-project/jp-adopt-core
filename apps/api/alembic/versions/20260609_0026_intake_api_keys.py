"""Add intake_api_key table for self-managed bearer credentials

Revision ID: 0026
Revises: 0025
Create Date: 2026-06-09

#59: replaces the static ``INTAKE_API_KEYS`` env-var allowlist with a
self-managed DB-backed credential store. Admins can mint and revoke
keys without an infra-team round-trip. Each key:

- Stores the bearer-token as a SHA-256 hash (raw never persisted)
- Carries a free-form ``consumer_label`` (e.g. ``"jp-adopt-forms
  production"``) and an optional note for operator context
- Records who minted it (``created_by_user_id``)
- Tracks last-use timestamp + IP + UA for an audit trail

The env-var allowlist stays valid as a fallback for the migration
window — see ``apps/api/src/jp_adopt_api/routers/intake.py``'s
``_authenticate`` for the look-up order. Drop the env-var path in a
follow-up release once every consumer has been moved to DB-issued
keys.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0026"
down_revision: str | Sequence[str] | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "intake_api_key",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("consumer_label", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_ip", sa.Text(), nullable=True),
        sa.Column("last_used_user_agent", sa.Text(), nullable=True),
    )
    # Unique key_hash so re-minting the same plaintext (extremely
    # unlikely; we generate from os.urandom) is rejected at the DB.
    op.create_index(
        "uq_intake_api_key_key_hash", "intake_api_key", ["key_hash"], unique=True
    )
    # Look-up index for the active-key check the auth path runs on every
    # intake request. Partial — revoked keys never match.
    op.create_index(
        "ix_intake_api_key_active_hash",
        "intake_api_key",
        ["key_hash"],
        unique=False,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_intake_api_key_active_hash", table_name="intake_api_key")
    op.drop_index("uq_intake_api_key_key_hash", table_name="intake_api_key")
    op.drop_table("intake_api_key")
