"""Drip engine for amy-return build (U10)

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-20

Adds the in-app drip engine's data model:

  * campaign — top-level marketing campaign (name + trigger + state).
    Status is one of draft|active|paused|archived. ``trigger_event_type``
    names an outbox event_type (e.g. ``jp.adopt.v1.match.accepted``) that
    fires enrollment. ``version`` increments when staff edit the campaign
    mid-flight so in-flight enrollments stay pinned to the version they
    started under.
  * campaign_step — ordered steps inside a campaign. ``delay_days`` is
    the relative offset from the prior step (or enrollment for position
    0). ``mjml_template_name`` is a filename in
    ``apps/api/email-templates/``, NOT inline content. ``send_at_hour``
    and ``send_at_minute`` define "send at 9am local" semantics.
  * enrollment — per-(campaign, contact) state row. ``state`` is one of
    pending|active|paused|completed|exited. Partial unique index on
    (campaign_id, contact_id) WHERE state IN (active,paused) enforces
    "one active enrollment per contact per campaign."
  * enrollment_event — append-only log of step_sent / paused / resumed /
    bounced / exited / etc. JSONB payload carries the per-event detail.
  * suppression_list — emails the engine must never send to. Checked at
    send-time as a hard filter. Keyed by email_hash (SHA-256 over the
    normalized email) so the table holds no raw PII.

The plan's earlier proposal had a separate ``sequence`` table between
campaign and step. Dropped here as premature — campaign points to steps
directly. Re-add if a campaign ever needs multiple parallel sequences.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- outbox: drip_processed_at -------------------------------------------
    # Mirrors the existing ``processed_at`` (webhook drain) and
    # ``claimed_at`` (in-flight). The drip enrollment drain uses this
    # column to mark events it has already consumed; the partial index
    # gives it a fast WHERE clause without scanning processed rows.
    op.add_column(
        "outbox",
        sa.Column(
            "drip_processed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_outbox_drip_unprocessed",
        "outbox",
        ["created_at"],
        postgresql_where=sa.text("drip_processed_at IS NULL"),
    )

    # --- campaign -------------------------------------------------------------
    op.create_table(
        "campaign",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column(
            "trigger_type",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'event'"),
        ),
        # Outbox event_type that triggers enrollment (when trigger_type='event').
        # Null when trigger_type='manual'.
        sa.Column("trigger_event_type", sa.Text(), nullable=True),
        # Auto-enroll existing contacts on activation when true; otherwise
        # only new outbox events drive enrollments.
        sa.Column(
            "auto_enroll_existing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        # Precedence used when a contact is eligible for multiple
        # campaigns. Higher number wins. Practical use: rank
        # transactional > onboarding > nurture so the daily-cap doesn't
        # silently drop a transactional message in favor of a nurture.
        sa.Column(
            "precedence",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # Edits to step content/timing bump this. In-flight enrollments
        # pin to the version they started under via
        # ``enrollment.campaign_version_id``.
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'paused', 'archived')",
            name="ck_campaign_status",
        ),
        sa.CheckConstraint(
            "trigger_type IN ('event', 'manual')",
            name="ck_campaign_trigger_type",
        ),
    )
    op.create_index(
        "ix_campaign_status",
        "campaign",
        ["status"],
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_campaign_trigger_event_type",
        "campaign",
        ["trigger_event_type"],
        postgresql_where=sa.text(
            "trigger_event_type IS NOT NULL AND status = 'active'"
        ),
    )

    # --- campaign_step --------------------------------------------------------
    op.create_table(
        "campaign_step",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campaign.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        # Relative delay from the prior step (or enrollment.enrolled_at for
        # position 0). Calendar days, not business days.
        sa.Column(
            "delay_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # MJML template filename in apps/api/email-templates/. Loaded from
        # disk at send time.
        sa.Column("mjml_template_name", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        # "Send at 9am local" — gated at send-time. 24-hour clock.
        sa.Column(
            "send_at_hour",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("9"),
        ),
        sa.Column(
            "send_at_minute",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "position >= 0",
            name="ck_campaign_step_position_nonneg",
        ),
        sa.CheckConstraint(
            "delay_days >= 0",
            name="ck_campaign_step_delay_days_nonneg",
        ),
        sa.CheckConstraint(
            "send_at_hour >= 0 AND send_at_hour <= 23",
            name="ck_campaign_step_send_at_hour_range",
        ),
        sa.CheckConstraint(
            "send_at_minute >= 0 AND send_at_minute <= 59",
            name="ck_campaign_step_send_at_minute_range",
        ),
    )
    # One step per position per campaign — prevents accidental dupes when
    # staff edit a campaign via UI.
    op.create_index(
        "uq_campaign_step_campaign_position",
        "campaign_step",
        ["campaign_id", "position"],
        unique=True,
    )

    # --- enrollment ----------------------------------------------------------
    op.create_table(
        "enrollment",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campaign.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Pin to the campaign's version at enroll time so mid-flight edits
        # don't change behavior for already-enrolled contacts.
        sa.Column(
            "campaign_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        # Current step position. -1 means "not yet started" (briefly true
        # between INSERT and the first send). 0 = first step about to fire.
        sa.Column(
            "current_step_position",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "enrolled_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Timestamp of the last successfully-sent step. Used by the
        # step-due query: next_due = last_step_sent_at + delay_days. For
        # position 0 (no prior step), use enrolled_at instead.
        sa.Column(
            "last_step_sent_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # Set when state transitions to completed/exited so we can compute
        # campaign-level retention metrics.
        sa.Column(
            "exited_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("exit_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'active', 'paused', 'completed', 'exited')",
            name="ck_enrollment_state",
        ),
        sa.CheckConstraint(
            "current_step_position >= -1",
            name="ck_enrollment_step_position_nonneg",
        ),
    )
    # At most one active|paused enrollment per (campaign, contact).
    # Completed/exited rows accumulate for audit; the partial index lets
    # a contact be re-enrolled after a prior cycle terminated.
    op.create_index(
        "uq_enrollment_open_per_campaign_contact",
        "enrollment",
        ["campaign_id", "contact_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('pending', 'active', 'paused')"),
    )
    op.create_index(
        "ix_enrollment_contact_id",
        "enrollment",
        ["contact_id"],
    )
    # Worker's step-due query: WHERE state='active' AND last_step_sent_at
    # IS NULL OR last_step_sent_at < now() - delay_days. The index helps
    # the active-state scan; the delay comparison happens against
    # campaign_step joined.
    op.create_index(
        "ix_enrollment_state_step",
        "enrollment",
        ["state", "current_step_position"],
        postgresql_where=sa.text("state = 'active'"),
    )

    # --- enrollment_event ----------------------------------------------------
    op.create_table(
        "enrollment_event",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=False),
            primary_key=True,
        ),
        sa.Column(
            "enrollment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("enrollment.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # step_sent | send_failed | paused | resumed | exited | bounced | unsubscribed
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_enrollment_event_enrollment_id",
        "enrollment_event",
        ["enrollment_id", "created_at"],
    )

    # --- suppression_list ----------------------------------------------------
    op.create_table(
        "suppression_list",
        # SHA-256 hex of the normalized email. Storing only the hash keeps
        # the table free of raw PII while still allowing the hot-path
        # lookup ``WHERE email_hash = :hash`` at send time.
        sa.Column("email_hash", sa.Text(), nullable=False),
        # hard_bounce | soft_bounce_exited | unsubscribed | manual | spam_complaint
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "suppressed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Free-form bag for the bounce-handler / Event Grid payload that
        # caused this entry so the operator can debug edge cases.
        sa.Column("source_metadata", postgresql.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("email_hash", name="pk_suppression_list"),
    )
    op.create_index(
        "ix_suppression_list_suppressed_at",
        "suppression_list",
        ["suppressed_at"],
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
                    SELECT unnest(ARRAY[
                        'campaign',
                        'campaign_step',
                        'enrollment',
                        'enrollment_event',
                        'suppression_list'
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
        "ix_suppression_list_suppressed_at", table_name="suppression_list"
    )
    op.drop_table("suppression_list")

    op.drop_index(
        "ix_enrollment_event_enrollment_id", table_name="enrollment_event"
    )
    op.drop_table("enrollment_event")

    op.drop_index("ix_enrollment_state_step", table_name="enrollment")
    op.drop_index("ix_enrollment_contact_id", table_name="enrollment")
    op.drop_index(
        "uq_enrollment_open_per_campaign_contact", table_name="enrollment"
    )
    op.drop_table("enrollment")

    op.drop_index(
        "uq_campaign_step_campaign_position", table_name="campaign_step"
    )
    op.drop_table("campaign_step")

    op.drop_index(
        "ix_campaign_trigger_event_type", table_name="campaign"
    )
    op.drop_index("ix_campaign_status", table_name="campaign")
    op.drop_table("campaign")

    op.drop_index("ix_outbox_drip_unprocessed", table_name="outbox")
    op.drop_column("outbox", "drip_processed_at")
