"""contact_profile: JP-custom adoption fields (DT parity, Group B / U6)

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-26

Adds a 1:1 ``contact_profile`` table holding the JP-custom adoption fields
registered by the ``dt-adoption-fields`` WordPress plugin (see
docs/dt-parity-inventory.md §2.6 A). Kept off ``contacts`` deliberately: those
fields mutate independently of the match/transition flows, and co-locating them
would churn ``contacts.version`` (the optimistic-lock column those flows gate
on). Status fields (adopter_status / facilitator_status) stay on ``contacts``;
per-FPG answers live on ``adopter_interest`` (0013).

All columns are nullable — a contact may have no profile yet, and intake fills
what the form provided. Enum-shaped columns carry ``IS NULL OR IN (...)`` CHECKs
mirrored from the plugin's option sets.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "contact_profile",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # --- contact_info tile ------------------------------------------------
        sa.Column("ministry_areas", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("entity_size", sa.Text(), nullable=True),
        sa.Column("primary_contact_name", sa.Text(), nullable=True),
        sa.Column("secondary_contact_name", sa.Text(), nullable=True),
        sa.Column("secondary_contact_email", sa.Text(), nullable=True),
        sa.Column("secondary_contact_phone", sa.Text(), nullable=True),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column("preferred_communication", sa.Text(), nullable=True),
        sa.Column("form_country", sa.Text(), nullable=True),
        sa.Column("form_state_region", sa.Text(), nullable=True),
        # --- adoption_profile tile -------------------------------------------
        sa.Column("adopter_type", sa.Text(), nullable=True),
        sa.Column("commitment_types", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("commitment_date", sa.Date(), nullable=True),
        # --- facilitation_profile tile ---------------------------------------
        sa.Column("works_with_fpgs", sa.Boolean(), nullable=True),
        sa.Column("willing_to_facilitate", sa.Boolean(), nullable=True),
        sa.Column(
            "facilitation_entity_types", postgresql.ARRAY(sa.Text()), nullable=True
        ),
        sa.Column(
            "facilitation_entity_sizes", postgresql.ARRAY(sa.Text()), nullable=True
        ),
        sa.Column("mou_status", sa.Text(), nullable=True),
        sa.Column("mou_signature_name", sa.Text(), nullable=True),
        # --- connection_prefs tile -------------------------------------------
        sa.Column("want_facilitator_connection", sa.Boolean(), nullable=True),
        sa.Column(
            "facilitator_entity_types", postgresql.ARRAY(sa.Text()), nullable=True
        ),
        sa.Column(
            "desired_facilitator_info", postgresql.ARRAY(sa.Text()), nullable=True
        ),
        # --- network_prefs tile ----------------------------------------------
        sa.Column("want_network_connection", sa.Boolean(), nullable=True),
        sa.Column("network_partner_info", postgresql.ARRAY(sa.Text()), nullable=True),
        # --- vetting tile ----------------------------------------------------
        sa.Column("has_doctrinal_distinctives", sa.Boolean(), nullable=True),
        sa.Column("doctrinal_distinctives", sa.Text(), nullable=True),
        sa.Column("has_accountability_membership", sa.Boolean(), nullable=True),
        sa.Column("accountability_memberships", sa.Text(), nullable=True),
        # --- engagement tile -------------------------------------------------
        sa.Column("last_contact_date", sa.Date(), nullable=True),
        sa.Column("engagement_score", sa.Integer(), nullable=True),
        sa.Column("next_followup_date", sa.Date(), nullable=True),
        # --- form_submission tile (referral/campaign/partner are readonly) ----
        sa.Column("referral_source", sa.Text(), nullable=True),
        sa.Column("campaign", sa.Text(), nullable=True),
        sa.Column("partner", sa.Text(), nullable=True),
        sa.Column("additional_notes", sa.Text(), nullable=True),
        sa.Column("file_download_url", sa.Text(), nullable=True),
        # --- timestamps ------------------------------------------------------
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
        # 1:1 with contacts.
        sa.UniqueConstraint("contact_id", name="uq_contact_profile_contact_id"),
        # Enum CHECKs — option sets mirror dt-adoption-fields/custom-fields.php.
        sa.CheckConstraint(
            "entity_size IS NULL OR entity_size IN "
            "('1', 'lt_30', '31_100', '101_500', '501_2000', '2001_plus')",
            name="ck_contact_profile_entity_size",
        ),
        sa.CheckConstraint(
            "preferred_communication IS NULL OR preferred_communication IN "
            "('email', 'phone')",
            name="ck_contact_profile_preferred_communication",
        ),
        sa.CheckConstraint(
            "adopter_type IS NULL OR adopter_type IN "
            "('individual', 'small_group', 'church', 'organization', 'network')",
            name="ck_contact_profile_adopter_type",
        ),
        sa.CheckConstraint(
            "mou_status IS NULL OR mou_status IN "
            "('signed', 'not_required', 'not_sent')",
            name="ck_contact_profile_mou_status",
        ),
        sa.CheckConstraint(
            "engagement_score IS NULL OR "
            "(engagement_score >= 0 AND engagement_score <= 100)",
            name="ck_contact_profile_engagement_score_range",
        ),
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jp_adopt_migrator') THEN
                EXECUTE 'ALTER TABLE contact_profile OWNER TO jp_adopt_migrator';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_table("contact_profile")
