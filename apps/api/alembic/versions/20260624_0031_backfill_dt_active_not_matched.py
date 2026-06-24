"""Backfill: DT 'active' adopters were mis-mapped to 'matched' — reset to 'engaged'

Revision ID: 0031
Revises: 0030
Create Date: 2026-06-24

No adopter was ever matched in Disciple.Tools, but the ETL status mapper
collapsed DT ``active`` into core ``matched`` (mappers/status.py). Result:
every ``adopter_status='matched'`` contact in core is really a DT ``active``
adopter, wrongly showing a green "Matched" badge.

Verified 2026-06-24 against prod: 203 contacts at ``adopter_status='matched'``,
ALL ``source_system='dt'``, 0 with a real ``match`` row. The mapper is fixed in
the same change (``active -> engaged``); this revision corrects the rows already
imported.

Scope guard: only touches DT-sourced contacts at 'matched' that have NO live
match row, so a genuine core-accepted match (which sets 'matched' via the state
machine and always has a match row) is never downshifted. This is a one-time
data correction, not a workflow transition, so it writes adopter_status
directly (the state-machine-via-transition rule governs the API, not backfills).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE contacts
            SET adopter_status = 'engaged'
            WHERE adopter_status = 'matched'
              AND source_system = 'dt'
              AND NOT EXISTS (
                  SELECT 1
                  FROM adopter_interest ai
                  JOIN "match" m ON m.adopter_interest_id = ai.id
                  WHERE ai.contact_id = contacts.id
              )
            """
        )
    )


def downgrade() -> None:
    # One-time data correction; not structurally reversible (we cannot tell which
    # 'engaged' rows were originally the mis-mapped 'matched' ones). No-op.
    pass
