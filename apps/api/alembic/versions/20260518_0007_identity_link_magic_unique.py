"""identity_link: unique partial index per (email_normalized, idp_name='magic_link')

Revision ID: 0007
Revises: 0004
Create Date: 2026-05-18

Fixes the magic-link claim race-window described in F1 of the U1-U6 PR
review: two concurrent claims for the same just-issued token would both
pass the ``claimed_at IS NULL`` check, and the second one would also
insert a duplicate ``identity_link`` row for the same email. The CAS
fix on ``magic_link_token.claimed_at`` covers token reuse; this index
prevents the duplicate ``identity_link`` insert from ever succeeding so
the database is the final fail-closed gate.

Pre-existing duplicates would block this index, so the upgrade first
deletes any duplicate magic-link IdentityLink rows, keeping the earliest
``linked_at`` per email. (The newer duplicates would have been minted by
the racey claim path; the older row is the one the rest of the system
already referenced.)
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Dedupe any existing duplicates first so the unique index can be created.
    # Keep the oldest row by linked_at (matches the new claim_magic_link
    # ORDER BY linked_at ASC LIMIT 1 lookup).
    op.execute(
        sa.text(
            """
            DELETE FROM identity_link a
            USING identity_link b
            WHERE a.idp_name = 'magic_link'
              AND b.idp_name = 'magic_link'
              AND a.email_normalized = b.email_normalized
              AND a.linked_at > b.linked_at
            """
        )
    )
    op.create_index(
        "uq_identity_link_magic_email",
        "identity_link",
        ["email_normalized"],
        unique=True,
        postgresql_where=sa.text("idp_name = 'magic_link'"),
    )

    # Own to migrator role when present (per-app DB user discipline).
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jp_adopt_migrator') THEN
                EXECUTE 'ALTER INDEX uq_identity_link_magic_email OWNER TO jp_adopt_migrator';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_index("uq_identity_link_magic_email", table_name="identity_link")
