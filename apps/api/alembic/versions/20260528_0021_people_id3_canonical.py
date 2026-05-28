"""Re-key fpg on people_id3 (destructive greenfield migration)

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-28

Prod has no real contacts/interests/matches yet; this deletes all fpg-related
rows and re-keys the schema. Operators re-run sync_fpg against the forms export
endpoint after applying.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Purge dependent rows (greenfield — no value backfill).
    op.execute(sa.text("DELETE FROM match_attempt"))
    op.execute(sa.text("DELETE FROM match"))
    op.execute(sa.text("DELETE FROM facilitator_fpg_coverage"))
    op.execute(sa.text("DELETE FROM adopter_interest"))
    op.execute(sa.text("DELETE FROM fpg"))

    # 2. Drop FKs and PKs that reference fpg.people_id3.
    op.drop_constraint(
        "adopter_interest_rop3_fkey", "adopter_interest", type_="foreignkey"
    )
    op.drop_constraint(
        "facilitator_fpg_coverage_rop3_fkey",
        "facilitator_fpg_coverage",
        type_="foreignkey",
    )
    op.drop_constraint("pk_facilitator_fpg_coverage", "facilitator_fpg_coverage")
    op.drop_constraint("fpg_pkey", "fpg", type_="primary")

    # 3. Drop rop3-era indexes.
    op.drop_index("ix_adopter_interest_rop3", table_name="adopter_interest")
    op.drop_index(
        "ix_facilitator_fpg_coverage_rop3", table_name="facilitator_fpg_coverage"
    )
    op.drop_index("ix_fpg_people_id3", table_name="fpg")

    # 4. Drop rop3 column; people_id3 becomes the PK.
    op.drop_column("fpg", "rop3")
    op.alter_column("fpg", "people_id3", existing_type=sa.Text(), nullable=False)
    op.create_primary_key("fpg_pkey", "fpg", ["people_id3"])

    # 5. Rename FK columns on child tables.
    op.alter_column(
        "adopter_interest",
        "rop3",
        new_column_name="people_id3",
        existing_type=sa.Text(),
        existing_nullable=True,
    )
    op.alter_column(
        "facilitator_fpg_coverage",
        "rop3",
        new_column_name="people_id3",
        existing_type=sa.Text(),
        existing_nullable=False,
    )

    # 6. Restore FKs and composite PK.
    op.create_foreign_key(
        "adopter_interest_people_id3_fkey",
        "adopter_interest",
        "fpg",
        ["people_id3"],
        ["people_id3"],
    )
    op.create_foreign_key(
        "facilitator_fpg_coverage_people_id3_fkey",
        "facilitator_fpg_coverage",
        "fpg",
        ["people_id3"],
        ["people_id3"],
        ondelete="CASCADE",
    )
    op.create_primary_key(
        "pk_facilitator_fpg_coverage",
        "facilitator_fpg_coverage",
        ["facilitator_org_id", "people_id3"],
    )
    op.create_index(
        "ix_adopter_interest_people_id3",
        "adopter_interest",
        ["people_id3"],
    )
    op.create_index(
        "ix_facilitator_fpg_coverage_people_id3",
        "facilitator_fpg_coverage",
        ["people_id3"],
    )

    # Re-load demo FPG + coverage rows (same AAA01..05 codes, now as people_id3 PK).
    op.execute(
        sa.text(
            """
            INSERT INTO fpg (people_id3, name, country_code, language_codes, frontier)
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
    op.execute(
        sa.text(
            """
            INSERT INTO facilitator_fpg_coverage (facilitator_org_id, people_id3)
            VALUES
                ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2', 'AAA02'),
                ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2', 'AAA03'),
                ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2', 'AAA05'),
                ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb3', 'AAA01'),
                ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb3', 'AAA03'),
                ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb3', 'AAA04')
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM match_attempt"))
    op.execute(sa.text("DELETE FROM match"))
    op.execute(sa.text("DELETE FROM facilitator_fpg_coverage"))
    op.execute(sa.text("DELETE FROM adopter_interest"))
    op.execute(sa.text("DELETE FROM fpg"))

    op.drop_index(
        "ix_facilitator_fpg_coverage_people_id3",
        table_name="facilitator_fpg_coverage",
    )
    op.drop_index("ix_adopter_interest_people_id3", table_name="adopter_interest")
    op.drop_constraint("pk_facilitator_fpg_coverage", "facilitator_fpg_coverage")
    op.drop_constraint(
        "facilitator_fpg_coverage_people_id3_fkey",
        "facilitator_fpg_coverage",
        type_="foreignkey",
    )
    op.drop_constraint(
        "adopter_interest_people_id3_fkey", "adopter_interest", type_="foreignkey"
    )

    op.alter_column(
        "facilitator_fpg_coverage",
        "people_id3",
        new_column_name="rop3",
        existing_type=sa.Text(),
        existing_nullable=False,
    )
    op.alter_column(
        "adopter_interest",
        "people_id3",
        new_column_name="rop3",
        existing_type=sa.Text(),
        existing_nullable=True,
    )

    op.drop_constraint("fpg_pkey", "fpg", type_="primary")
    op.add_column("fpg", sa.Column("rop3", sa.Text(), nullable=True))
    op.alter_column("fpg", "people_id3", existing_type=sa.Text(), nullable=True)
    op.create_primary_key("fpg_pkey", "fpg", ["rop3"])

    op.create_foreign_key(
        "facilitator_fpg_coverage_rop3_fkey",
        "facilitator_fpg_coverage",
        "fpg",
        ["rop3"],
        ["rop3"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "adopter_interest_rop3_fkey",
        "adopter_interest",
        "fpg",
        ["rop3"],
        ["rop3"],
    )
    op.create_primary_key(
        "pk_facilitator_fpg_coverage",
        "facilitator_fpg_coverage",
        ["facilitator_org_id", "rop3"],
    )
    op.create_index(
        "ix_facilitator_fpg_coverage_rop3",
        "facilitator_fpg_coverage",
        ["rop3"],
    )
    op.create_index("ix_adopter_interest_rop3", "adopter_interest", ["rop3"])
    op.create_index(
        "ix_fpg_people_id3",
        "fpg",
        ["people_id3"],
        unique=False,
        postgresql_where=sa.text("people_id3 IS NOT NULL"),
    )
