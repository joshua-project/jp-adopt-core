"""foundation migration for amy-return build (U1)

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-15

Adds:
  * Contact columns (version, b2c_subject_id, email_normalized,
    source_system, source_id, local_modified_after_import, origin,
    newsletter_opt_in, country_code, language_codes)
  * CHECK constraints on contacts.adopter_status and contacts.facilitator_status
  * New tables: roles (with 4 seeded), user_roles, transition_audit,
    identity_link, partner_tenants, migration_conflicts
"""

from __future__ import annotations

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ADOPTER_STATUS_VALUES = (
    "draft",
    "new",
    "potential_adopter",
    "contacted",
    "engaged",
    "matched",
    "sent_back",
    "active",
    "inactive",
    "do_not_engage",
)

FACILITATOR_STATUS_VALUES = (
    "draft",
    "new",
    "not_ready",
    "ready",
    "do_not_engage",
)


def _quoted_in_list(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    # --- contacts: new columns ------------------------------------------------
    op.add_column(
        "contacts",
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column(
        "contacts",
        sa.Column("b2c_subject_id", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_contacts_b2c_subject_id",
        "contacts",
        ["b2c_subject_id"],
    )
    op.add_column(
        "contacts",
        sa.Column("email_normalized", sa.Text(), nullable=True),
    )
    op.create_index(
        "uq_contacts_email_normalized",
        "contacts",
        ["email_normalized"],
        unique=True,
        postgresql_where=sa.text("email_normalized IS NOT NULL"),
    )
    op.add_column(
        "contacts",
        sa.Column("source_system", sa.Text(), nullable=True),
    )
    op.add_column(
        "contacts",
        sa.Column("source_id", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_contacts_source_system_source_id",
        "contacts",
        ["source_system", "source_id"],
    )
    op.add_column(
        "contacts",
        sa.Column(
            "local_modified_after_import",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "contacts",
        sa.Column("origin", sa.Text(), nullable=True),
    )
    op.add_column(
        "contacts",
        sa.Column(
            "newsletter_opt_in",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "contacts",
        sa.Column("country_code", sa.Text(), nullable=True),
    )
    op.add_column(
        "contacts",
        sa.Column(
            "language_codes",
            postgresql.ARRAY(sa.Text()),
            nullable=True,
        ),
    )

    # --- contacts: CHECK constraints -----------------------------------------
    # Migrate the spike-era seed value 'new_inquiry' to the canonical 'new'
    # before applying the CHECK constraint. Dev/CI Postgres started on 0001
    # has a single seeded contact in this state; the constraint below would
    # otherwise reject any pre-existing row.
    op.execute(
        "UPDATE contacts SET adopter_status = 'new' "
        "WHERE adopter_status = 'new_inquiry'"
    )
    adopter_values_sql = _quoted_in_list(ADOPTER_STATUS_VALUES)
    op.create_check_constraint(
        "ck_contacts_adopter_status",
        "contacts",
        f"adopter_status IS NULL OR adopter_status IN ({adopter_values_sql})",
    )
    op.create_check_constraint(
        "ck_contacts_facilitator_status",
        "contacts",
        (
            "facilitator_status IS NULL OR facilitator_status IN ("
            f"{_quoted_in_list(FACILITATOR_STATUS_VALUES)})"
        ),
    )

    # --- roles ----------------------------------------------------------------
    op.create_table(
        "roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # --- user_roles -----------------------------------------------------------
    op.create_table(
        "user_roles",
        sa.Column("user_b2c_subject_id", sa.Text(), nullable=False),
        sa.Column(
            "role_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("roles.id"),
            nullable=False,
        ),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_b2c_subject_id", "role_id"),
    )

    # --- transition_audit -----------------------------------------------------
    op.create_table(
        "transition_audit",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id"),
            nullable=False,
        ),
        sa.Column("from_state", sa.Text(), nullable=True),
        sa.Column("to_state", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("actor_role", sa.Text(), nullable=True),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("reason_text", sa.Text(), nullable=True),
        sa.Column(
            "outbox_event_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=True,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_transition_audit_contact_id",
        "transition_audit",
        ["contact_id"],
    )

    # --- identity_link --------------------------------------------------------
    op.create_table(
        "identity_link",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("b2c_subject_id", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("email_normalized", sa.Text(), nullable=False),
        sa.Column("idp_name", sa.Text(), nullable=False),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_identity_link_email_normalized",
        "identity_link",
        ["email_normalized"],
    )
    op.create_index(
        "uq_identity_link_b2c_subject_id",
        "identity_link",
        ["b2c_subject_id"],
        unique=True,
        postgresql_where=sa.text("b2c_subject_id IS NOT NULL"),
    )

    # --- partner_tenants ------------------------------------------------------
    op.create_table(
        "partner_tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("microsoft_tenant_id", sa.Text(), nullable=False, unique=True),
        sa.Column("partner_id", sa.Text(), nullable=True),
        sa.Column("partner_name", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # --- migration_conflicts --------------------------------------------------
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "migration_conflicts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_system", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("table_name", sa.Text(), nullable=False),
        sa.Column("conflict_type", sa.Text(), nullable=False),
        sa.Column("source_value", jsonb, nullable=True),
        sa.Column("local_value", jsonb, nullable=True),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_migration_conflicts_source_table",
        "migration_conflicts",
        ["source_system", "source_id", "table_name"],
    )

    # --- seed 4 roles ---------------------------------------------------------
    # The plan dropped facilitator_admin and adoption_partner as dead-code roles
    # for week 1; only staff_admin, adoption_manager, triage_facilitator,
    # and facilitator are seeded here.
    seed_sql = sa.text(
        """
        INSERT INTO roles (id, name, description)
        VALUES (:id, :name, :description)
        """
    )
    bind = op.get_bind()
    for name, description in (
        (
            "staff_admin",
            "Full platform access; manages roles and configuration",
        ),
        (
            "adoption_manager",
            "Triages adopters, makes matches, reviews send-backs (Amy's role)",
        ),
        (
            "triage_facilitator",
            "Default queue assignee for adopters with no FPG selected",
        ),
        (
            "facilitator",
            "Receives matched adopters; accepts/declines/sends-back",
        ),
    ):
        bind.execute(
            seed_sql,
            {"id": uuid.uuid4(), "name": name, "description": description},
        )


def downgrade() -> None:
    # Reverse order of upgrade().
    op.drop_index(
        "ix_migration_conflicts_source_table",
        table_name="migration_conflicts",
    )
    op.drop_table("migration_conflicts")

    op.drop_table("partner_tenants")

    op.drop_index("uq_identity_link_b2c_subject_id", table_name="identity_link")
    op.drop_index("ix_identity_link_email_normalized", table_name="identity_link")
    op.drop_table("identity_link")

    op.drop_index("ix_transition_audit_contact_id", table_name="transition_audit")
    op.drop_table("transition_audit")

    op.drop_table("user_roles")
    op.drop_table("roles")

    op.drop_constraint("ck_contacts_facilitator_status", "contacts", type_="check")
    op.drop_constraint("ck_contacts_adopter_status", "contacts", type_="check")

    op.drop_column("contacts", "language_codes")
    op.drop_column("contacts", "country_code")
    op.drop_column("contacts", "newsletter_opt_in")
    op.drop_column("contacts", "origin")
    op.drop_column("contacts", "local_modified_after_import")
    op.drop_index("ix_contacts_source_system_source_id", table_name="contacts")
    op.drop_column("contacts", "source_id")
    op.drop_column("contacts", "source_system")
    op.drop_index("uq_contacts_email_normalized", table_name="contacts")
    op.drop_column("contacts", "email_normalized")
    op.drop_index("ix_contacts_b2c_subject_id", table_name="contacts")
    op.drop_column("contacts", "b2c_subject_id")
    op.drop_column("contacts", "version")
