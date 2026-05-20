"""DT migration target tables for amy-return build (U9)

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-20

Adds the target tables the DT MySQL → Postgres ETL writes into:

  * activity_log — DT wp_comments + wp_dt_activity_log mapped per contact
    with author attribution and threading. Most DT communication history
    lands here so Amy retains comment-level context post-cutover.
  * staff_identity_link — maps DT wp_users → B2C subject IDs (when
    available) + email so authored activity_log rows can resolve back to
    the right human. Built first, then referenced by activity_log inserts.
  * etl_run — one row per ETL invocation, recording timing, counts, and
    the source watermark so incremental delta runs can resume from the
    last successful checkpoint.
  * etl_deleted_in_source — rows that disappeared from DT MySQL between
    snapshots. ETL does NOT hard-delete from Postgres; it reports here so
    Amy can review.

Idempotency:
  * Every imported Contact / AdopterInterest / activity_log row carries
    (source_system='dt', source_id=<wp_post_id|wp_comment_id|...>).
  * ON CONFLICT DO UPDATE … WHERE local_modified_after_import = false
    keeps re-runs from clobbering staff edits.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- contacts: partial unique index on (source_system, source_id) ---------
    # U1 added a regular non-unique index on this pair. ETL's idempotent
    # ``ON CONFLICT (source_system, source_id) DO UPDATE`` requires a
    # unique (or unique partial) index to target. Local-origin rows (no
    # source) and DT-imported rows coexist; partial WHERE excludes the
    # locals from the uniqueness check so duplicate-NULL source_id rows
    # remain legal.
    op.create_index(
        "uq_contacts_source_system_source_id",
        "contacts",
        ["source_system", "source_id"],
        unique=True,
        postgresql_where=sa.text(
            "source_system IS NOT NULL AND source_id IS NOT NULL"
        ),
    )

    # --- staff_identity_link --------------------------------------------------
    op.create_table(
        "staff_identity_link",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("dt_user_id", sa.Text(), nullable=False),
        sa.Column("b2c_subject_id", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("email_normalized", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        # active | inactive | unknown — captured from wp_users at import.
        # "unknown" is what we use when a comment author's wp_user row is
        # missing (deleted user case); we still create a link row so the
        # activity_log author_id has somewhere to point.
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "source_system",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'dt'"),
        ),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive', 'unknown')",
            name="ck_staff_identity_link_status",
        ),
    )
    op.create_index(
        "uq_staff_identity_link_dt_user_id",
        "staff_identity_link",
        ["dt_user_id"],
        unique=True,
    )
    op.create_index(
        "uq_staff_identity_link_b2c_subject_id",
        "staff_identity_link",
        ["b2c_subject_id"],
        unique=True,
        postgresql_where=sa.text("b2c_subject_id IS NOT NULL"),
    )
    op.create_index(
        "ix_staff_identity_link_email_normalized",
        "staff_identity_link",
        ["email_normalized"],
    )

    # --- activity_log ---------------------------------------------------------
    op.create_table(
        "activity_log",
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
        # author_id is a string. Real DT users come through via
        # staff_identity_link.id; the synthetic "system:dt_legacy_unknown"
        # sentinel covers comments whose wp_users row was deleted.
        sa.Column("author_id", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        # comment_type from wp_comments (often empty string in WP for plain
        # comments). Preserved verbatim for filtering downstream.
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column(
            "parent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("activity_log.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # source_system + source_id are required on every row so the ETL
        # idempotency contract holds. Locally-authored rows (new comments
        # written via the app post-cutover) would use source_system='local'.
        sa.Column(
            "source_system",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'local'"),
        ),
        sa.Column("source_id", sa.Text(), nullable=True),
        # Free-form bag for the bits of wp_comments we don't model
        # individually (comment_agent, comment_IP if we ever need it, etc.).
        sa.Column(
            "source_metadata",
            postgresql.JSONB(),
            nullable=True,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_activity_log_contact_id",
        "activity_log",
        ["contact_id"],
    )
    op.create_index(
        "ix_activity_log_parent_id",
        "activity_log",
        ["parent_id"],
        postgresql_where=sa.text("parent_id IS NOT NULL"),
    )
    # ETL idempotency: (source_system, source_id) is the natural key for
    # imported rows. Local rows have source_id NULL and are excluded from
    # the unique check (partial index).
    op.create_index(
        "uq_activity_log_source_system_source_id",
        "activity_log",
        ["source_system", "source_id"],
        unique=True,
        postgresql_where=sa.text("source_id IS NOT NULL"),
    )
    op.create_index(
        "ix_activity_log_occurred_at",
        "activity_log",
        ["occurred_at"],
    )

    # --- etl_run --------------------------------------------------------------
    op.create_table(
        "etl_run",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("table_name", sa.Text(), nullable=False),
        # dry_run | production
        sa.Column(
            "mode",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'production'"),
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "ended_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # The MAX(updated_at) the source MySQL had at the time the snapshot
        # was taken. Next delta run uses this as the WHERE clause floor.
        sa.Column(
            "source_max_modified_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # If the run was a delta from a prior watermark, record it here so
        # the audit row carries both endpoints.
        sa.Column(
            "watermark_from",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "rows_in",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "rows_out_inserted",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "rows_out_updated",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "rows_out_skipped",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "rows_in_conflict",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "errors",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "mode IN ('dry_run', 'production')",
            name="ck_etl_run_mode",
        ),
    )
    op.create_index(
        "ix_etl_run_table_started",
        "etl_run",
        ["table_name", "started_at"],
    )

    # --- etl_deleted_in_source ------------------------------------------------
    op.create_table(
        "etl_deleted_in_source",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "etl_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("etl_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("table_name", sa.Text(), nullable=False),
        sa.Column("source_system", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_etl_deleted_in_source_run",
        "etl_deleted_in_source",
        ["etl_run_id"],
    )
    op.create_index(
        "ix_etl_deleted_in_source_source",
        "etl_deleted_in_source",
        ["source_system", "source_id", "table_name"],
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
                        'staff_identity_link',
                        'activity_log',
                        'etl_run',
                        'etl_deleted_in_source'
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
        "ix_etl_deleted_in_source_source",
        table_name="etl_deleted_in_source",
    )
    op.drop_index(
        "ix_etl_deleted_in_source_run",
        table_name="etl_deleted_in_source",
    )
    op.drop_table("etl_deleted_in_source")

    op.drop_index("ix_etl_run_table_started", table_name="etl_run")
    op.drop_table("etl_run")

    op.drop_index("ix_activity_log_occurred_at", table_name="activity_log")
    op.drop_index(
        "uq_activity_log_source_system_source_id",
        table_name="activity_log",
    )
    op.drop_index("ix_activity_log_parent_id", table_name="activity_log")
    op.drop_index("ix_activity_log_contact_id", table_name="activity_log")
    op.drop_table("activity_log")

    op.drop_index(
        "ix_staff_identity_link_email_normalized",
        table_name="staff_identity_link",
    )
    op.drop_index(
        "uq_staff_identity_link_b2c_subject_id",
        table_name="staff_identity_link",
    )
    op.drop_index(
        "uq_staff_identity_link_dt_user_id",
        table_name="staff_identity_link",
    )
    op.drop_table("staff_identity_link")

    op.drop_index(
        "uq_contacts_source_system_source_id",
        table_name="contacts",
    )
