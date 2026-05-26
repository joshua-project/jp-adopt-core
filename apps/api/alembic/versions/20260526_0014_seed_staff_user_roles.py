"""Seed staff Entra OIDs in user_roles (U21 of the Entra direct plan)

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-26

Without this seed, every Entra-authenticated user receives an empty role
frozenset from ``deps.load_user_roles`` and 403s on every ``require_role``-
gated endpoint (post-U22 that is every contacts route + admin + matches +
workflow + drips + manual_contacts). The seed bootstraps Joel (the director)
as ``staff_admin`` so the system is operable at launch; further role
assignments happen via direct SQL or the deferred admin UI (Part F).

Idempotent: ``ON CONFLICT DO NOTHING`` against the composite PK
``(user_subject_id, role_id)``. The role row already exists in foundation
migration 0003 (deterministic UUID lookup via ``role.name``).

If you want to add another staff Entra OID, append a tuple to
``_STAFF_SEEDS`` below. Look up the OID via Azure portal (Entra → Users →
Object ID) or ``az ad user show --id <upn>@joshuaproject.net --query id -o tsv``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Alembic ID metadata
revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# (Entra OID, role_name) tuples. Operator adds new staff here in a later
# revision (don't edit this migration after it has applied — new revision).
_STAFF_SEEDS: tuple[tuple[str, str], ...] = (
    ("546dce1f-9e3b-422d-a938-f5a9437f164e", "staff_admin"),  # Joel Castillo (joel.castillo@joshuaproject.net)
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
