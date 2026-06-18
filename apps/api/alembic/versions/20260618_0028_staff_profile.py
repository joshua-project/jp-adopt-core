"""Create staff_profile table for digest recipient resolution

Revision ID: 0028
Revises: 0027
Create Date: 2026-06-18

Decouples the daily-digest staff recipient lookup from the ``contacts``
table.

Prior to this revision,
``apps/api/src/jp_adopt_api/domain/digest.py:_load_staff_recipients``
resolved staff emails by joining ``user_roles`` → ``contacts`` on
``b2c_subject_id``. Launch staff with only a ``user_roles`` row (Joel
in 0014, Amy in 0015) were silently skipped because contacts had no
matching row. Migration 0027 worked around this by inserting a
``staff_seed`` Contact row per staff member, which is the wrong shape
— ``contacts`` is for adopters and facilitators, not internal staff.

This revision introduces a dedicated ``staff_profile`` table that
carries the email + display name keyed by ``b2c_subject_id``. The
digest pipeline joins ``user_roles`` → ``staff_profile`` and skips
staff who have no profile (preserving the existing fail-safe).

Seeded with Joel + Amy so this revision is self-contained for the
prod cutover; future staff onboarding adds a row here alongside the
``user_roles`` seed.

The ``staff_seed`` Contact rows from 0027 are left in place — they're
benign once the digest no longer reads them, and removing them in the
same revision adds blast radius without value. A follow-up can clean
them up after this is verified in prod.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Alembic ID metadata
revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# (b2c_subject_id, display_name, email) — same identities as 0027.
_STAFF_SEEDS: tuple[tuple[str, str, str], ...] = (
    (
        "546dce1f-9e3b-422d-a938-f5a9437f164e",  # Joel Castillo (0014)
        "Joel Castillo",
        "joel.castillo@joshuaproject.net",
    ),
    (
        "c3c8a516-4d53-4336-a1c1-ceb56fbb9d7c",  # Amy Banta (0015)
        "Amy Banta",
        "amy.banta@globalspecifics.com",
    ),
)


def upgrade() -> None:
    op.create_table(
        "staff_profile",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("b2c_subject_id", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("email_normalized", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "digest_opt_in",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "b2c_subject_id", name="uq_staff_profile_b2c_subject_id"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="ck_staff_profile_status",
        ),
    )
    op.create_index(
        "ix_staff_profile_email_normalized",
        "staff_profile",
        ["email_normalized"],
    )

    for oid, name, email in _STAFF_SEEDS:
        op.execute(
            sa.text(
                """
                INSERT INTO staff_profile (
                    b2c_subject_id, email, email_normalized, display_name
                )
                VALUES (:oid, :email, :email_norm, :name)
                ON CONFLICT (b2c_subject_id) DO NOTHING
                """
            ).bindparams(oid=oid, email=email, email_norm=email.lower(), name=name)
        )


def downgrade() -> None:
    op.drop_index("ix_staff_profile_email_normalized", table_name="staff_profile")
    op.drop_table("staff_profile")
