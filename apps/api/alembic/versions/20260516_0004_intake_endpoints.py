"""intake endpoints + outbox suppression support (U4)

Adds:
  * ``api_idempotency_keys`` — request-deduplication table for intake endpoints.
    Unique on (api_key_id, key); stores the cached response body and the SHA-256
    of the request body for collision detection (same key + different body →
    422 idempotency_key_conflict, mirroring jp-adopt-forms).
  * ``submissions_blocked`` — anti-enumeration log. Submissions that resolve to
    a ``do_not_engage`` contact are silently dropped (201 Created — matches
    the accepted-first-call status to avoid a status-code oracle) but written here
    for Amy to review. Returning 403 would let a third party probe for
    blocked-list membership.

Note on revision sequencing: ``down_revision = "0006"`` stacks this migration
on top of the magic-link side-car (0006), which already shipped. The chain is
therefore 0001 → 0002 → 0003 → 0005 → 0006 → 0004 (head). The numeric prefix
"0004" matches the unit ID and the original plan's filename — Alembic uses the
``revision`` string for ordering, not the filename, so the apparent
out-of-order numbering is cosmetic only.

Revision ID: 0004
Revises: 0006
Create Date: 2026-05-16

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── api_idempotency_keys ───────────────────────────────────────────────
    op.create_table(
        "api_idempotency_keys",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("api_key_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column(
            "response_body",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            # Plan's "24h dedup window" — overridable per-request later via TTL
            # extension; default at insert time is 24h forward.
            server_default=sa.text("now() + interval '24 hours'"),
        ),
        sa.UniqueConstraint(
            "api_key_id", "key", name="uq_api_idempotency_keys_apikey_key"
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'completed')",
            name="ck_api_idempotency_keys_state",
        ),
    )
    op.create_index(
        "ix_api_idempotency_keys_expires_at",
        "api_idempotency_keys",
        ["expires_at"],
    )

    # ── submissions_blocked ────────────────────────────────────────────────
    op.create_table(
        "submissions_blocked",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("email_normalized", sa.Text(), nullable=True),
        # 'do_not_engage' is the only v1 reason. Kept as TEXT (not enum) so
        # future reasons (e.g. 'rate_limited', 'spam_pattern') don't need an
        # ALTER TYPE.
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "submission_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "blocked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_submissions_blocked_email_normalized",
        "submissions_blocked",
        ["email_normalized"],
    )
    op.create_index(
        "ix_submissions_blocked_contact_id",
        "submissions_blocked",
        ["contact_id"],
    )

    # Own to migrator role when present (per-app DB user discipline).
    op.execute(
        """
        DO $$
        DECLARE
            t text;
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jp_adopt_migrator') THEN
                FOR t IN
                    SELECT unnest(ARRAY[
                        'api_idempotency_keys',
                        'submissions_blocked'
                    ])
                LOOP
                    EXECUTE format('ALTER TABLE %I OWNER TO jp_adopt_migrator', t);
                END LOOP;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_submissions_blocked_contact_id", table_name="submissions_blocked"
    )
    op.drop_index(
        "ix_submissions_blocked_email_normalized", table_name="submissions_blocked"
    )
    op.drop_table("submissions_blocked")
    op.drop_index(
        "ix_api_idempotency_keys_expires_at", table_name="api_idempotency_keys"
    )
    op.drop_table("api_idempotency_keys")
