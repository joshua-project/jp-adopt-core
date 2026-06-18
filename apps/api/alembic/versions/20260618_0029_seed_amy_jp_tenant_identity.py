"""Seed Amy Banta's joshuaproject.net identity (user_roles + staff_profile)

Revision ID: 0029
Revises: 0028
Create Date: 2026-06-18

During the 2026-06-18 demo, Amy signed in with her joshuaproject.net
account — a different Entra OID than her globalspecifics.com identity
seeded in 0014/0015/0028. Her role was granted by a direct INSERT into
``user_roles`` in production to unblock the demo. This revision codifies
that grant in migration history (so a rebuild/restore reproduces it) and
adds the matching ``staff_profile`` row so the daily digest reaches her.

Safe to re-run: both inserts use ON CONFLICT DO NOTHING. In production the
``user_roles`` grant already exists (applied directly during the demo), so
that insert is a no-op; the ``staff_profile`` row for the JP OID does not
yet exist, so that insert creates it — the intended effect, so the digest
reaches her JP mailbox.

It also opts the older globalspecifics.com ``staff_profile`` (seeded in
0028) out of the digest. That account was the mistaken setup, now
corrected to joshuaproject.net; without this she would receive the daily
digest at both mailboxes (the recipient query dedupes per-email, not
per-person). She remains an active staff member there — only digest
delivery is disabled.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Alembic ID metadata
revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_OID = "77fb39e1-3acd-4012-bd8d-2a2a34534dc1"  # Amy Banta (amy.banta@joshuaproject.net)
_ROLE = "staff_admin"
_DISPLAY_NAME = "Amy Banta"
_EMAIL = "amy.banta@joshuaproject.net"

# Amy's prior (mistaken) identity, seeded in 0015/0028. Opt it out of the
# digest so she isn't emailed at both mailboxes.
_OLD_OID = "c3c8a516-4d53-4336-a1c1-ceb56fbb9d7c"  # amy.banta@globalspecifics.com


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO user_roles (user_subject_id, role_id)
            SELECT :oid, id FROM roles WHERE name = :role
            ON CONFLICT (user_subject_id, role_id) DO NOTHING
            """
        ).bindparams(oid=_OID, role=_ROLE)
    )
    op.execute(
        sa.text(
            """
            INSERT INTO staff_profile (
                b2c_subject_id, email, email_normalized, display_name
            )
            VALUES (:oid, :email, :email_norm, :name)
            ON CONFLICT (b2c_subject_id) DO NOTHING
            """
        ).bindparams(
            oid=_OID, email=_EMAIL, email_norm=_EMAIL.lower(), name=_DISPLAY_NAME
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE staff_profile
            SET digest_opt_in = false
            WHERE b2c_subject_id = :old_oid
            """
        ).bindparams(old_oid=_OLD_OID)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE staff_profile
            SET digest_opt_in = true
            WHERE b2c_subject_id = :old_oid
            """
        ).bindparams(old_oid=_OLD_OID)
    )
    op.execute(
        sa.text(
            "DELETE FROM staff_profile WHERE b2c_subject_id = :oid"
        ).bindparams(oid=_OID)
    )
    op.execute(
        sa.text(
            """
            DELETE FROM user_roles
            WHERE user_subject_id = :oid
              AND role_id IN (SELECT id FROM roles WHERE name = :role)
            """
        ).bindparams(oid=_OID, role=_ROLE)
    )
