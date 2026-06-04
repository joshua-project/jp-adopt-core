"""Add contacts.phone (DT comm-channel migration target)

Revision ID: 0023
Revises: 0022
Create Date: 2026-06-03

DT stores phone numbers as ``contact_phone_<hash>`` comm-channel postmeta.
The DT ETL maps the primary into this single nullable column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("contacts", sa.Column("phone", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("contacts", "phone")
