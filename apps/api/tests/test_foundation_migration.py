"""Tests for U1 foundation migration (0003_foundation_amy_return).

These tests assume `alembic upgrade head` has run before the test session
(conftest is invoked after the dev DB is migrated). They verify the
post-migration schema shape using raw SQL through an async engine.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from jp_adopt_api.config import get_settings

SEED_CONTACT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

EXPECTED_ROLES = (
    "adoption_manager",
    "facilitator",
    "staff_admin",
    "triage_facilitator",
)


@pytest.fixture
async def conn() -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as connection:
        yield connection
        # Each test rolls back its own work, but be defensive.
        await connection.rollback()
    await engine.dispose()


async def test_contacts_has_new_columns(conn: AsyncConnection) -> None:
    """Migration adds the expected columns to contacts."""
    result = await conn.execute(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'contacts'
            """
        )
    )
    columns = {row[0] for row in result.all()}
    expected = {
        "version",
        "b2c_subject_id",
        "email_normalized",
        "source_system",
        "source_id",
        "local_modified_after_import",
        "origin",
        "newsletter_opt_in",
        "country_code",
        "language_codes",
    }
    assert expected.issubset(columns), f"missing: {expected - columns}"


async def test_new_tables_exist(conn: AsyncConnection) -> None:
    result = await conn.execute(
        text(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            """
        )
    )
    tables = {row[0] for row in result.all()}
    expected = {
        "roles",
        "user_roles",
        "transition_audit",
        "identity_link",
        "partner_tenants",
        "migration_conflicts",
    }
    assert expected.issubset(tables), f"missing: {expected - tables}"


async def test_seeded_roles_exact_set(conn: AsyncConnection) -> None:
    """Only the 4 plan-blessed roles are seeded — facilitator_admin and
    adoption_partner are intentionally dropped for week 1."""
    result = await conn.execute(text("SELECT name FROM roles ORDER BY name"))
    names = tuple(row[0] for row in result.all())
    assert names == EXPECTED_ROLES


async def test_unique_partial_email_normalized_blocks_dupes(
    conn: AsyncConnection,
) -> None:
    """UNIQUE partial index on contacts.email_normalized rejects duplicates
    but allows multiple NULL email_normalized rows."""
    a = uuid.uuid4()
    b = uuid.uuid4()
    insert_sql = text(
        "INSERT INTO contacts (id, party_kind, display_name, email_normalized) "
        "VALUES (:id, 'adopter', :name, 'dup@example.com')"
    )
    async with conn.begin():
        await conn.execute(insert_sql, {"id": a, "name": "A"})
        with pytest.raises(IntegrityError):
            await conn.execute(insert_sql, {"id": b, "name": "B"})

    # Two NULL email_normalized rows must both succeed.
    c = uuid.uuid4()
    d = uuid.uuid4()
    async with conn.begin():
        await conn.execute(
            text(
                "INSERT INTO contacts (id, party_kind, display_name) "
                "VALUES (:id, 'adopter', 'C')"
            ),
            {"id": c},
        )
        await conn.execute(
            text(
                "INSERT INTO contacts (id, party_kind, display_name) "
                "VALUES (:id, 'adopter', 'D')"
            ),
            {"id": d},
        )
        # Clean up so concurrent runs don't accumulate noise.
        await conn.execute(
            text("DELETE FROM contacts WHERE id IN (:c, :d)"),
            {"c": c, "d": d},
        )


async def test_check_constraint_rejects_invalid_adopter_status(
    conn: AsyncConnection,
) -> None:
    """CHECK ck_contacts_adopter_status rejects values outside the enum,
    and accepts a valid one ('potential_adopter')."""
    insert_with_status = text(
        "INSERT INTO contacts (id, party_kind, display_name, adopter_status) "
        "VALUES (:id, 'adopter', :name, :status)"
    )
    bad = uuid.uuid4()
    async with conn.begin():
        with pytest.raises(IntegrityError):
            await conn.execute(
                insert_with_status,
                {"id": bad, "name": "Banana", "status": "banana"},
            )

    good = uuid.uuid4()
    async with conn.begin():
        await conn.execute(
            insert_with_status,
            {"id": good, "name": "Pa", "status": "potential_adopter"},
        )
        await conn.execute(
            text("DELETE FROM contacts WHERE id = :id"), {"id": good}
        )


async def test_check_constraint_rejects_adopter_value_in_facilitator_column(
    conn: AsyncConnection,
) -> None:
    """'matched' is an adopter-side status; facilitator_status must reject it."""
    rid = uuid.uuid4()
    insert_fac = text(
        "INSERT INTO contacts "
        "(id, party_kind, display_name, facilitator_status) "
        "VALUES (:id, 'facilitator', 'F', 'matched')"
    )
    async with conn.begin():
        with pytest.raises(IntegrityError):
            await conn.execute(insert_fac, {"id": rid})


async def test_transition_audit_roundtrip(conn: AsyncConnection) -> None:
    audit_id = uuid.uuid4()
    async with conn.begin():
        await conn.execute(
            text(
                """
                INSERT INTO transition_audit
                    (id, contact_id, from_state, to_state, actor_id, actor_role,
                     reason_code, reason_text)
                VALUES
                    (:id, :contact_id, 'new', 'potential_adopter',
                     'user:test', 'adoption_manager', 'triage', 'looks promising')
                """
            ),
            {"id": audit_id, "contact_id": SEED_CONTACT_ID},
        )
        result = await conn.execute(
            text(
                "SELECT from_state, to_state, reason_code "
                "FROM transition_audit WHERE id = :id"
            ),
            {"id": audit_id},
        )
        row = result.one()
        assert row.from_state == "new"
        assert row.to_state == "potential_adopter"
        assert row.reason_code == "triage"
        await conn.execute(
            text("DELETE FROM transition_audit WHERE id = :id"),
            {"id": audit_id},
        )


async def test_identity_link_magic_dedup_handles_equal_linked_at(
    conn: AsyncConnection,
) -> None:
    """N7: migration 0007's dedup query used to be
    ``WHERE a.linked_at > b.linked_at``. Two duplicate rows with identical
    ``linked_at`` would both fail that check and both survive, so the
    subsequent ``CREATE UNIQUE INDEX`` blew up. The fix adds an
    ``OR (a.linked_at = b.linked_at AND a.id > b.id)`` UUID tiebreaker so
    exactly one row of every equal-timestamp pair is removed.

    We can't run ``alembic downgrade 0004 → upgrade head`` from within a
    test without disturbing the shared test DB, so this test exercises the
    same SQL against a transactional scratch table that mirrors the
    relevant ``identity_link`` columns. If the SQL is correct here it is
    correct in the migration.
    """
    a = uuid.uuid4()
    b = uuid.uuid4()
    email = f"dedup-{uuid.uuid4().hex[:8]}@example.com"
    async with conn.begin():
        await conn.execute(
            text(
                """
                CREATE TEMP TABLE _il_dedup (
                    id uuid PRIMARY KEY,
                    email_normalized text NOT NULL,
                    idp_name text NOT NULL,
                    linked_at timestamptz NOT NULL
                ) ON COMMIT DROP
                """
            )
        )
        # Two rows for the same (email, idp_name='magic_link') with the
        # same linked_at — what the old WHERE clause failed on.
        await conn.execute(
            text(
                """
                INSERT INTO _il_dedup (id, email_normalized, idp_name, linked_at)
                VALUES
                    (:a, :e, 'magic_link', '2026-05-18 12:00:00+00'),
                    (:b, :e, 'magic_link', '2026-05-18 12:00:00+00')
                """
            ),
            {"a": a, "b": b, "e": email},
        )
        # The migration's fixed dedup SQL.
        await conn.execute(
            text(
                """
                DELETE FROM _il_dedup a
                USING _il_dedup b
                WHERE a.id != b.id
                  AND a.idp_name = 'magic_link'
                  AND b.idp_name = 'magic_link'
                  AND a.email_normalized = b.email_normalized
                  AND (a.linked_at > b.linked_at
                       OR (a.linked_at = b.linked_at AND a.id > b.id))
                """
            )
        )
        remaining = (
            await conn.execute(
                text(
                    "SELECT id FROM _il_dedup WHERE email_normalized = :e"
                ),
                {"e": email},
            )
        ).all()
        assert len(remaining) == 1, (
            f"expected exactly one survivor with tied linked_at, got "
            f"{len(remaining)}"
        )
        # Survivor must be the smaller UUID — that's the deterministic
        # rule the tiebreaker enforces.
        survivor_id = remaining[0][0]
        assert survivor_id == min(a, b)


async def test_identity_link_unique_b2c_subject_id(conn: AsyncConnection) -> None:
    sub = f"oid:{uuid.uuid4()}"
    a = uuid.uuid4()
    b = uuid.uuid4()
    async with conn.begin():
        await conn.execute(
            text(
                """
                INSERT INTO identity_link
                    (id, b2c_subject_id, email, email_normalized, idp_name)
                VALUES
                    (:id, :sub, 'x@example.com', 'x@example.com', 'entra')
                """
            ),
            {"id": a, "sub": sub},
        )
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    """
                    INSERT INTO identity_link
                        (id, b2c_subject_id, email, email_normalized, idp_name)
                    VALUES
                        (:id, :sub, 'y@example.com', 'y@example.com', 'entra')
                    """
                ),
                {"id": b, "sub": sub},
            )
