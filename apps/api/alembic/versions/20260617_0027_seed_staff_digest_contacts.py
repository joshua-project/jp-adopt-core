"""Seed Contact rows for launch staff so the daily digest reaches them.

Revision ID: 0027
Revises: 0026
Create Date: 2026-06-17

The daily-digest recipient lookup
(``apps/api/src/jp_adopt_api/domain/digest.py:_load_staff_recipients``)
joins ``user_roles`` → ``contacts`` via ``b2c_subject_id`` and
silently skips staff who have no Contact row. Joel (migration 0014)
and Amy (migration 0015) have user_roles entries but no Contact, so
neither receives the digest today.

This seed inserts minimal Contact rows for both, distinguished by
``party_kind='staff'`` and ``source_system='staff_seed'`` so they don't
get picked up by adopter/facilitator lists. It's a workaround pending
the proper decoupling of the staff-recipient lookup from ``contacts``
(follow-up migration 0028).

Idempotent: NOT EXISTS guard on ``email_normalized`` — re-running on a
DB that already has a Contact for either email is a no-op.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Alembic ID metadata
revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# (b2c_subject_id, display_name, email_normalized) — emails are already
# lower-cased to match the partial unique index on email_normalized.
_STAFF_CONTACT_SEEDS: tuple[tuple[str, str, str], ...] = (
    (
        "546dce1f-9e3b-422d-a938-f5a9437f164e",  # Joel Castillo (migration 0014)
        "Joel Castillo",
        "joel.castillo@joshuaproject.net",
    ),
    (
        "c3c8a516-4d53-4336-a1c1-ceb56fbb9d7c",  # Amy Banta (migration 0015)
        "Amy Banta",
        "amy.banta@globalspecifics.com",
    ),
)


def upgrade() -> None:
    # contacts.id has no server default — the ORM supplies uuid.uuid4()
    # at insert time, but raw SQL bypasses that. Use gen_random_uuid()
    # (built-in on PG14+) so the seed runs from Alembic.
    for oid, name, email in _STAFF_CONTACT_SEEDS:
        op.execute(
            sa.text(
                """
                INSERT INTO contacts (
                    id, party_kind, display_name,
                    b2c_subject_id, email_normalized,
                    source_system, source_id
                )
                SELECT gen_random_uuid(), 'staff', :name, :oid, :email,
                       'staff_seed', :oid
                WHERE NOT EXISTS (
                    SELECT 1 FROM contacts WHERE email_normalized = :email
                )
                """
            ).bindparams(oid=oid, name=name, email=email)
        )


def downgrade() -> None:
    for _, _, email in _STAFF_CONTACT_SEEDS:
        op.execute(
            sa.text(
                """
                DELETE FROM contacts
                WHERE source_system = 'staff_seed'
                  AND email_normalized = :email
                """
            ).bindparams(email=email)
        )
