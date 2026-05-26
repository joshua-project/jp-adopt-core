"""Rename user_b2c_subject_id -> user_subject_id (U20 of the Entra direct plan)

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-26

B2C is dead; staff sign in via Entra direct (Phase 2 / jp-adopt-core#60).
The columns are not B2C-specific — they hold whatever subject identifier
(Entra OID, B2C subject, magic-link sub) the API resolved at request time.
Rename for honest naming.

Two columns affected, on different tables:

  * user_roles.user_b2c_subject_id        -> user_subject_id
  * facilitator_org_membership.user_b2c_subject_id -> user_subject_id

Both are in-place renames via ``op.alter_column(new_column_name=...)``;
existing rows are preserved.

The composite PRIMARY KEY on each table includes the renamed column.
PostgreSQL preserves the constraint identity through ALTER COLUMN RENAME —
the constraint stays named ``pk_user_roles`` (default) and
``pk_facilitator_org_membership`` (explicit, set by migration 0008), and
they continue to reference the renamed column without further action. No
``ALTER TABLE ... RENAME CONSTRAINT`` is needed.

This migration ships in the same PR as the ORM rename (``models.py``),
the dependency-injection update (``deps.py``), the digest domain query
update (``domain/digest.py``), the admin router contract change
(``routers/admin.py`` — request/response Pydantic field name + DELETE
path parameter name), and the org-scope guard updates
(``routers/workflow.py``, ``routers/matches.py``). Partial deploy (DB
migrated, app still references the old attribute name) would 500 on every
role lookup; PR-level atomicity is the gate.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# Alembic ID metadata
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "user_roles",
        "user_b2c_subject_id",
        new_column_name="user_subject_id",
    )
    op.alter_column(
        "facilitator_org_membership",
        "user_b2c_subject_id",
        new_column_name="user_subject_id",
    )


def downgrade() -> None:
    op.alter_column(
        "facilitator_org_membership",
        "user_subject_id",
        new_column_name="user_b2c_subject_id",
    )
    op.alter_column(
        "user_roles",
        "user_subject_id",
        new_column_name="user_b2c_subject_id",
    )
