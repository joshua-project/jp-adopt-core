"""facilitator role surface for amy-return build (U8)

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-18

Adds:
  * facilitator_org_membership (user_b2c_subject_id, facilitator_org_id):
    M:N link letting the API filter the facilitator-portal queue to the
    orgs a signed-in facilitator belongs to. The plan calls for the
    portal to show ``Match`` rows where the actor belongs to
    ``facilitator_org_id``; week 1 keeps the model thin — a simple join
    table seeded by staff_admin.
  * facilitator_outbox_subscriptions (per-org outbox webhook endpoints):
    optional per-org HMAC webhook destinations. Drained by the existing
    outbox worker. Empty in week 1; populated as facilitator orgs come
    online with their own systems.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- facilitator_org_membership ------------------------------------------
    op.create_table(
        "facilitator_org_membership",
        sa.Column(
            "user_b2c_subject_id",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "facilitator_org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("facilitating_org.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role_in_org",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'member'"),
        ),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "user_b2c_subject_id",
            "facilitator_org_id",
            name="pk_facilitator_org_membership",
        ),
        # F33: enumerate allowed values so a future writer can't insert
        # an unmodeled role string and silently bypass authz checks.
        sa.CheckConstraint(
            "role_in_org IN ('member', 'admin')",
            name="ck_facilitator_org_membership_role_in_org",
        ),
    )
    op.create_index(
        "ix_facilitator_org_membership_facilitator_org_id",
        "facilitator_org_membership",
        ["facilitator_org_id"],
    )

    # --- facilitator_outbox_subscriptions ------------------------------------
    op.create_table(
        "facilitator_outbox_subscriptions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "facilitator_org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("facilitating_org.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Glob pattern for matching event_type values. ``*`` matches every
        # event type; ``jp.adopt.v1.match.*`` matches every match-prefixed
        # event. The worker drain interprets this at delivery time.
        sa.Column(
            "event_type_glob",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'jp.adopt.v1.match.*'"),
        ),
        sa.Column("endpoint_url", sa.Text(), nullable=False),
        # F10: ``hmac_key`` is plain Text in week 1. The table is empty
        # because no admin endpoint inserts rows yet, and
        # ``Settings.enable_facilitator_outbox_subscriptions`` defaults to
        # False so the future admin surface refuses to write until v2.
        # v2 will migrate this column to a Key Vault reference (``kv://``
        # URI or similar) rather than a literal secret.
        sa.Column("hmac_key", sa.Text(), nullable=False),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
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
    )
    op.create_index(
        "ix_facilitator_outbox_subscriptions_org",
        "facilitator_outbox_subscriptions",
        ["facilitator_org_id"],
    )
    op.create_index(
        "ix_facilitator_outbox_subscriptions_active",
        "facilitator_outbox_subscriptions",
        ["active"],
        postgresql_where=sa.text("active = TRUE"),
    )

    # Per-app DB user discipline — re-own to migrator role when present.
    op.execute(
        """
        DO $$
        DECLARE
            t text;
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jp_adopt_migrator') THEN
                FOR t IN
                    SELECT unnest(ARRAY[
                        'facilitator_org_membership',
                        'facilitator_outbox_subscriptions'
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
        "ix_facilitator_outbox_subscriptions_active",
        table_name="facilitator_outbox_subscriptions",
    )
    op.drop_index(
        "ix_facilitator_outbox_subscriptions_org",
        table_name="facilitator_outbox_subscriptions",
    )
    op.drop_table("facilitator_outbox_subscriptions")
    op.drop_index(
        "ix_facilitator_org_membership_facilitator_org_id",
        table_name="facilitator_org_membership",
    )
    op.drop_table("facilitator_org_membership")
