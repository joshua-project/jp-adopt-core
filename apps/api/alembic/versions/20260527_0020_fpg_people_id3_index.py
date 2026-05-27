"""fpg.people_id3 lookup index (U12)

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-27

Intake resolves a submitted ``people_id3`` to its ``rop3`` via
``SELECT rop3 FROM fpg WHERE people_id3 = :x`` (routers/intake.py
``_resolve_rop3``). Once the fpg sync (scripts/sync_fpg.py) loads the real
~3.5k frontier people groups, that lookup runs on every form submission, so
index the column. Partial (people_id3 IS NOT NULL) since unsynced/demo rows
leave it NULL and never match a lookup.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_fpg_people_id3",
        "fpg",
        ["people_id3"],
        unique=False,
        postgresql_where="people_id3 IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_index("ix_fpg_people_id3", table_name="fpg")
