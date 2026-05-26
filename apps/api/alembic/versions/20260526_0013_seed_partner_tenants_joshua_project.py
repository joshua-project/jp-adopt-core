"""Seed partner_tenants with the Joshua Project Entra tenant (U13 of the Entra direct plan)

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-26

The Entra-direct decoder (apps/api/src/jp_adopt_api/auth_entra.py) validates
incoming JWTs against the `partner_tenants` allowlist by ``tid`` (the
issuer's tenant ID). Unknown tenants get ``403 tenant_not_provisioned``.
Without this seed, every JP staff member's first sign-in returns 403 even
though everything else is configured correctly.

Idempotent: ``ON CONFLICT (microsoft_tenant_id) DO NOTHING``. Uses Python-
generated UUID via ``uuid.uuid4()`` bound param — not ``gen_random_uuid()``
— because ``pgcrypto`` is not enabled in this repo's migrations and
``gen_random_uuid()`` as a core function is PG14+. Matches the seed pattern
used by ``20260515_0003_foundation_amy_return.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Alembic ID metadata
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_JP_TENANT_ID = "761e2c5f-34bd-4872-b86c-3a9f3b29d63a"


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO partner_tenants (id, microsoft_tenant_id, partner_id, partner_name)
            VALUES (CAST(:id AS UUID), :tid, :pid, :pname)
            ON CONFLICT (microsoft_tenant_id) DO NOTHING
            """
        ).bindparams(
            id=str(uuid.uuid4()),
            tid=_JP_TENANT_ID,
            pid="joshua-project",
            pname="Joshua Project",
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM partner_tenants WHERE microsoft_tenant_id = :tid"
        ).bindparams(tid=_JP_TENANT_ID)
    )
