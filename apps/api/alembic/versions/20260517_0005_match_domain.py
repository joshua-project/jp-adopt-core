"""match domain model for amy-return build (U5)

Revision ID: 0005
Revises: 0003
Create Date: 2026-05-15

Note on revision sequencing: 0005 follows 0003 directly because U4
(``20260516_0004_intake_endpoints.py``) is reserved but hasn't landed yet.
When U4 ships, its migration will re-thread the chain (either by setting
its own ``down_revision`` to 0003 and updating this file's
``down_revision`` to 0004, or via a merge revision). Until then, 0005
applies straight on top of 0003.

Adds:
  * facilitating_org (with capacity tracking, accepting_potential_adopters
    flag, and a UNIQUE partial index that enforces "exactly one triage org")
  * fpg (Joshua Project people group, rop3-keyed)
  * facilitator_fpg_coverage (M:N between facilitating_org and fpg)
  * adopter_interest (one row per (contact, FPG-or-no-FPG) submission)
  * match (with status CHECK + one-open-match-per-interest UNIQUE partial)
  * match_attempt (score breakdown + filter results audit trail)

Seeds:
  * 3 facilitating_orgs (Triage Queue, Example Mission Network,
    Frontier Adoption Alliance) with deterministic UUIDs ...bbb1/2/3
  * 5 demo FPGs (rop3 AAA01-AAA05)
  * 6 coverage rows
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


MATCH_STATUS_VALUES = (
    "recommended",
    "accepted",
    "sent_back",
    "declined",
    "active",
    "completed",
    "withdrawn",
    "triage",
)

OPEN_MATCH_STATUSES = ("recommended", "accepted", "active", "triage")


def _quoted_in_list(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    # --- facilitating_org -----------------------------------------------------
    op.create_table(
        "facilitating_org",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("country_code", sa.Text(), nullable=True),
        sa.Column(
            "language_codes",
            postgresql.ARRAY(sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "theological_tags",
            postgresql.ARRAY(sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "capacity_total",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "capacity_committed",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "accepting_potential_adopters",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "is_triage_org",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("last_assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_system", sa.Text(), nullable=True),
        sa.Column("source_id", sa.Text(), nullable=True),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "capacity_committed >= 0",
            name="ck_facilitating_org_capacity_committed_nonneg",
        ),
        sa.CheckConstraint(
            "capacity_committed <= capacity_total",
            name="ck_facilitating_org_capacity_committed_le_total",
        ),
    )
    op.create_index(
        "ix_facilitating_org_active_accepting",
        "facilitating_org",
        ["active", "accepting_potential_adopters"],
    )
    op.create_index(
        "ix_facilitating_org_source_system_source_id",
        "facilitating_org",
        ["source_system", "source_id"],
    )
    # Load-bearing: enforces "exactly one triage org" in v1.
    op.create_index(
        "uq_facilitating_org_is_triage_org",
        "facilitating_org",
        ["is_triage_org"],
        unique=True,
        postgresql_where=sa.text("is_triage_org = TRUE"),
    )

    # --- fpg ------------------------------------------------------------------
    op.create_table(
        "fpg",
        sa.Column("rop3", sa.Text(), primary_key=True),
        sa.Column("people_id3", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("country_code", sa.Text(), nullable=True),
        sa.Column(
            "language_codes",
            postgresql.ARRAY(sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "frontier",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_fpg_country_code", "fpg", ["country_code"])
    op.create_index(
        "ix_fpg_frontier",
        "fpg",
        ["frontier"],
        postgresql_where=sa.text("frontier = TRUE"),
    )

    # --- facilitator_fpg_coverage ---------------------------------------------
    op.create_table(
        "facilitator_fpg_coverage",
        sa.Column(
            "facilitator_org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("facilitating_org.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "rop3",
            sa.Text(),
            sa.ForeignKey("fpg.rop3", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "facilitator_org_id", "rop3", name="pk_facilitator_fpg_coverage"
        ),
    )
    op.create_index(
        "ix_facilitator_fpg_coverage_rop3",
        "facilitator_fpg_coverage",
        ["rop3"],
    )

    # --- adopter_interest -----------------------------------------------------
    op.create_table(
        "adopter_interest",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "rop3",
            sa.Text(),
            sa.ForeignKey("fpg.rop3"),
            nullable=True,
        ),
        sa.Column("commitment_level", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_adopter_interest_contact_id",
        "adopter_interest",
        ["contact_id"],
    )
    op.create_index(
        "ix_adopter_interest_rop3",
        "adopter_interest",
        ["rop3"],
    )

    # --- match ----------------------------------------------------------------
    op.create_table(
        "match",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "adopter_interest_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("adopter_interest.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "facilitator_org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("facilitating_org.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "recommended_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decision_reason_code", sa.Text(), nullable=True),
        sa.Column("decision_reason_text", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"status IN ({_quoted_in_list(MATCH_STATUS_VALUES)})",
            name="ck_match_status",
        ),
    )
    op.create_index("ix_match_adopter_interest_id", "match", ["adopter_interest_id"])
    op.create_index("ix_match_facilitator_org_id", "match", ["facilitator_org_id"])
    op.create_index("ix_match_status", "match", ["status"])
    # Load-bearing: one open match per adopter_interest.
    open_status_sql = ", ".join(f"'{s}'" for s in OPEN_MATCH_STATUSES)
    op.create_index(
        "uq_match_open_per_interest",
        "match",
        ["adopter_interest_id"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({open_status_sql})"),
    )

    # --- match_attempt --------------------------------------------------------
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "match_attempt",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "adopter_interest_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("adopter_interest.id"),
            nullable=True,
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "candidate_facilitator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("facilitating_org.id"),
            nullable=False,
        ),
        sa.Column("score", sa.Numeric(4, 3), nullable=True),
        sa.Column("score_breakdown", jsonb, nullable=True),
        sa.Column("filter_results", jsonb, nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_match_attempt_contact_id", "match_attempt", ["contact_id"])
    op.create_index("ix_match_attempt_run_id", "match_attempt", ["run_id"])
    op.create_index(
        "ix_match_attempt_contact_run",
        "match_attempt",
        ["contact_id", "run_id"],
    )

    # --- seed data ------------------------------------------------------------
    # Deterministic UUIDs so tests/runbooks can reference them.
    triage_org_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1"
    example_mission_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2"
    frontier_alliance_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb3"

    op.execute(
        sa.text(
            """
            INSERT INTO facilitating_org (
                id, name, country_code, language_codes, theological_tags,
                capacity_total, capacity_committed,
                accepting_potential_adopters, is_triage_org, active
            ) VALUES
                (CAST(:triage_id AS UUID), 'Triage Queue', NULL, NULL, NULL,
                 999, 0, TRUE, TRUE, TRUE),
                (CAST(:ex_id AS UUID), 'Example Mission Network', 'US',
                 ARRAY['en','es']::text[],
                 ARRAY['evangelical','baptist']::text[],
                 10, 0, TRUE, FALSE, TRUE),
                (CAST(:fa_id AS UUID), 'Frontier Adoption Alliance', 'US',
                 ARRAY['en']::text[],
                 ARRAY['evangelical','non_denominational']::text[],
                 5, 0, FALSE, FALSE, TRUE)
            """
        ).bindparams(
            triage_id=triage_org_id,
            ex_id=example_mission_id,
            fa_id=frontier_alliance_id,
        )
    )

    op.execute(
        sa.text(
            """
            INSERT INTO fpg (rop3, name, country_code, language_codes, frontier)
            VALUES
                ('AAA01', 'Demo FPG North Africa', 'MA',
                 ARRAY['ar']::text[], TRUE),
                ('AAA02', 'Demo FPG Central Asia', 'KZ',
                 ARRAY['kk','ru']::text[], TRUE),
                ('AAA03', 'Demo FPG South Asia', 'IN',
                 ARRAY['hi','en']::text[], TRUE),
                ('AAA04', 'Demo FPG Sub-Saharan Africa', 'ML',
                 ARRAY['fr','bm']::text[], TRUE),
                ('AAA05', 'Demo FPG Southeast Asia', 'ID',
                 ARRAY['id']::text[], TRUE)
            """
        )
    )

    # 6 coverage rows:
    #   Example Mission Network covers AAA02, AAA03, AAA05.
    #   Frontier Adoption Alliance covers AAA01, AAA03, AAA04.
    # (Triage org covers nothing; its is_triage_org flag is what routes
    # adopters with no FPG to it.)
    op.execute(
        sa.text(
            """
            INSERT INTO facilitator_fpg_coverage (facilitator_org_id, rop3)
            VALUES
                (CAST(:ex_id AS UUID), 'AAA02'),
                (CAST(:ex_id AS UUID), 'AAA03'),
                (CAST(:ex_id AS UUID), 'AAA05'),
                (CAST(:fa_id AS UUID), 'AAA01'),
                (CAST(:fa_id AS UUID), 'AAA03'),
                (CAST(:fa_id AS UUID), 'AAA04')
            """
        ).bindparams(
            ex_id=example_mission_id,
            fa_id=frontier_alliance_id,
        )
    )


def downgrade() -> None:
    # Reverse order of upgrade().
    op.drop_index("ix_match_attempt_contact_run", table_name="match_attempt")
    op.drop_index("ix_match_attempt_run_id", table_name="match_attempt")
    op.drop_index("ix_match_attempt_contact_id", table_name="match_attempt")
    op.drop_table("match_attempt")

    op.drop_index("uq_match_open_per_interest", table_name="match")
    op.drop_index("ix_match_status", table_name="match")
    op.drop_index("ix_match_facilitator_org_id", table_name="match")
    op.drop_index("ix_match_adopter_interest_id", table_name="match")
    op.drop_table("match")

    op.drop_index("ix_adopter_interest_rop3", table_name="adopter_interest")
    op.drop_index("ix_adopter_interest_contact_id", table_name="adopter_interest")
    op.drop_table("adopter_interest")

    op.drop_index(
        "ix_facilitator_fpg_coverage_rop3",
        table_name="facilitator_fpg_coverage",
    )
    op.drop_table("facilitator_fpg_coverage")

    op.drop_index("ix_fpg_frontier", table_name="fpg")
    op.drop_index("ix_fpg_country_code", table_name="fpg")
    op.drop_table("fpg")

    op.drop_index(
        "uq_facilitating_org_is_triage_org", table_name="facilitating_org"
    )
    op.drop_index(
        "ix_facilitating_org_source_system_source_id",
        table_name="facilitating_org",
    )
    op.drop_index(
        "ix_facilitating_org_active_accepting", table_name="facilitating_org"
    )
    op.drop_table("facilitating_org")
