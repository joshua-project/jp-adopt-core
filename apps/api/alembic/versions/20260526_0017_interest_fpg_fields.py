"""adopter_interest per-FPG fields (DT parity, Group B / U7)

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-26

Per-FPG answers from the intake forms (jp-adopt-forms) attach to the adopter's
FPG selection, not the contact. Adoption form rows carry ``commitment_types``;
facilitation form rows carry ``engagement_status`` (readiness),
``facilitation_services``, and ``network_services``. All nullable; existing
rows unaffected. ``engagement_status`` mirrors the form enum (ready/potential/none).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "adopter_interest",
        sa.Column("commitment_types", postgresql.ARRAY(sa.Text()), nullable=True),
    )
    op.add_column(
        "adopter_interest",
        sa.Column("engagement_status", sa.Text(), nullable=True),
    )
    op.add_column(
        "adopter_interest",
        sa.Column("facilitation_services", postgresql.ARRAY(sa.Text()), nullable=True),
    )
    op.add_column(
        "adopter_interest",
        sa.Column("network_services", postgresql.ARRAY(sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        "ck_adopter_interest_engagement_status",
        "adopter_interest",
        "engagement_status IS NULL OR engagement_status IN "
        "('ready', 'potential', 'none')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_adopter_interest_engagement_status",
        "adopter_interest",
        type_="check",
    )
    op.drop_column("adopter_interest", "network_services")
    op.drop_column("adopter_interest", "facilitation_services")
    op.drop_column("adopter_interest", "engagement_status")
    op.drop_column("adopter_interest", "commitment_types")
