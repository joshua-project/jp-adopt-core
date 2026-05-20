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
    #
    # N7: the original WHERE was just ``a.linked_at > b.linked_at``. If two
    # duplicate rows share an identical ``linked_at`` (which can happen when
    # two concurrent claims commit in the same millisecond on platforms
    # where Postgres rounds to microsecond precision and the application
    # uses ``now()`` from the same statement), neither side of the inequality
    # is true and both survive, blowing up the subsequent CREATE INDEX with
    # a unique-constraint violation. Add a UUID tiebreaker so exactly one
    # row of every equal-timestamp pair is deleted regardless of clock
    # resolution.
    op.execute(
        sa.text(
            """
            DELETE FROM identity_link a
            USING identity_link b
            WHERE a.id != b.id
              AND a.idp_name = 'magic_link'
              AND b.idp_name = 'magic_link'
              AND a.email_normalized = b.email_normalized
              AND (a.linked_at > b.linked_at
                   OR (a.linked_at = b.linked_at AND a.id > b.id))
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

    # N7 (DM-NEW-003): a previous revision of this migration also issued
    # ``ALTER INDEX uq_identity_link_magic_email OWNER TO jp_adopt_migrator``.
    # That statement is a no-op in Postgres — index access is governed by
    # the underlying table's ACL, not the index's owner, and the owner is
    # already inherited from whoever ran CREATE INDEX. Removed so future
    # readers don't add it back; the table-level OWNER TO in 0006 / 0003
    # already covers operational access.


def downgrade() -> None:
    op.drop_index("uq_identity_link_magic_email", table_name="identity_link")
