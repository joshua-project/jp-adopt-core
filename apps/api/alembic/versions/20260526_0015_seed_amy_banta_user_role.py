"""Seed Amy Banta's Entra OID as staff_admin (launch staff #2)

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-26

Adds the second launch staff member to ``user_roles`` so she can use role-
gated endpoints once she signs in. Amy is a JP-tenant member with UPN on
the verified secondary domain ``globalspecifics.com`` (not a B2B guest),
so her token's ``tid`` is the JP tenant ID and the existing
``partner_tenants`` row from 0013 admits her.

Same pattern as 0014. Future staff additions follow the same shape — one
revision per onboarding event (see ``docs/runbooks/multi-idp-b2c.md``).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Alembic ID metadata
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_STAFF_SEEDS: tuple[tuple[str, str], ...] = (
    ("c3c8a516-4d53-4336-a1c1-ceb56fbb9d7c", "staff_admin"),  # Amy Banta (amy.banta@globalspecifics.com)
)


def upgrade() -> None:
    for oid, role_name in _STAFF_SEEDS:
        op.execute(
            sa.text(
                """
                INSERT INTO user_roles (user_subject_id, role_id)
                SELECT :oid, id FROM roles WHERE name = :role
                ON CONFLICT (user_subject_id, role_id) DO NOTHING
                """
            ).bindparams(oid=oid, role=role_name)
        )


def downgrade() -> None:
    for oid, role_name in _STAFF_SEEDS:
        op.execute(
            sa.text(
                """
                DELETE FROM user_roles
                WHERE user_subject_id = :oid
                  AND role_id = (SELECT id FROM roles WHERE name = :role)
                """
            ).bindparams(oid=oid, role=role_name)
        )
