"""Daily digest tables for amy-return build (U11)

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-20

Adds the audit tables for the daily-digest job:

  * digest_run — one row per cron invocation; carries the send window
    + status + counts.
  * digest_recipient — one row per (digest_run, recipient) so we can
    answer "who got which day's digest, and what did it contain?" The
    ``match_ids`` JSONB column lists the match UUIDs that appeared in
    that recipient's email.

The digest task itself reads ``match`` rows transitioned in the last
24h and groups them by recipient (Amy gets all; facilitators get
their own org's). Idempotent on (window_start, recipient_address) so a
re-run on the same day doesn't double-send.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- digest_run ----------------------------------------------------------
    op.create_table(
        "digest_run",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        # Inclusive lower / exclusive upper of the digest window. Set
        # by the worker at run time (typically [yesterday 09:00 ET,
        # today 09:00 ET)).
        sa.Column(
            "window_start", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "window_end", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "ended_at", sa.DateTime(timezone=True), nullable=True
        ),
        # pending | sent | failed | empty
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "recipient_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "match_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'empty')",
            name="ck_digest_run_status",
        ),
    )
    op.create_index(
        "ix_digest_run_window_start",
        "digest_run",
        ["window_start"],
    )

    # --- digest_recipient ----------------------------------------------------
    op.create_table(
        "digest_recipient",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "digest_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("digest_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Email destination — used as the idempotency natural key alongside
        # the run's window_start. We do NOT hash here because the operator
        # wants to see "did Amy get it?" without a lookup.
        sa.Column("recipient_address", sa.Text(), nullable=False),
        # all_staff | facilitator | adoption_manager — frames the recipient
        # for the renderer so the email body can read "your matches" vs
        # "all matches today".
        sa.Column("recipient_kind", sa.Text(), nullable=False),
        # Facilitator org id when recipient_kind='facilitator'; null for
        # all_staff / adoption_manager rows. Used to scope the match list.
        sa.Column(
            "facilitator_org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("facilitating_org.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "match_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # The actual UUIDs that landed in this recipient's email body.
        # JSONB array of strings so we can replay or audit.
        sa.Column(
            "match_ids",
            postgresql.JSONB(),
            nullable=True,
        ),
        # sent | failed | skipped (e.g. no matches for this recipient)
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "recipient_kind IN ('all_staff', 'adoption_manager', 'facilitator')",
            name="ck_digest_recipient_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'skipped')",
            name="ck_digest_recipient_status",
        ),
    )
    op.create_index(
        "ix_digest_recipient_run",
        "digest_recipient",
        ["digest_run_id"],
    )
    # Idempotency: at most one row per (run, recipient_address).
    op.create_index(
        "uq_digest_recipient_run_address",
        "digest_recipient",
        ["digest_run_id", "recipient_address"],
        unique=True,
    )

    # --- ownership ------------------------------------------------------------
    op.execute(
        """
        DO $$
        DECLARE
            t text;
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jp_adopt_migrator') THEN
                FOR t IN
                    SELECT unnest(ARRAY['digest_run', 'digest_recipient'])
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
        "uq_digest_recipient_run_address", table_name="digest_recipient"
    )
    op.drop_index("ix_digest_recipient_run", table_name="digest_recipient")
    op.drop_table("digest_recipient")
    op.drop_index("ix_digest_run_window_start", table_name="digest_run")
    op.drop_table("digest_run")
