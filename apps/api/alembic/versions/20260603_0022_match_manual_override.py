"""match.is_manual_override (F1 / #52)

Revision ID: 0022
Revises: 0021
Create Date: 2026-06-03

Staff can hand-assign a match to a facilitator the algorithm did not
recommend (closing the override half of #52). Flag those matches so:
  * accept can bypass the capacity ceiling for an override (routers/matches.py),
  * overrides are auditable / reportable without reverse-engineering a NULL
    match_attempt score.

Additive boolean, NOT NULL DEFAULT false. Existing matches are all
algorithmic, so the default backfills them correctly with no data migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "match",
        sa.Column(
            "is_manual_override",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("match", "is_manual_override")
