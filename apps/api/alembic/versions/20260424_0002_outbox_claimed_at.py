"""outbox claimed_at for two-phase delivery

Revision ID: 20260424_0002
Revises: 20260423_0001
Create Date: 2026-04-24

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outbox",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("outbox", "claimed_at")
