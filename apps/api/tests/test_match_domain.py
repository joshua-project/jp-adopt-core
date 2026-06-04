"""Tests for U5 match domain migration (0005_match_domain).

These tests assume ``alembic upgrade head`` has been run before the test
session (the migration's seed data is checked too). Each test opens a
top-level transaction with ``conn.begin()`` so the connection auto-rolls
back on exit; constraint-violation tests use a *fresh* transaction per
failing INSERT (because asyncpg aborts the transaction on the first
IntegrityError, and nested savepoint RELEASE then fails) which keeps the
test pattern flat and predictable.

Deterministic seed UUIDs (see migration 0005):
    Triage Queue                 : ...bbb1
    Example Mission Network      : ...bbb2
    Frontier Adoption Alliance   : ...bbb3
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from jp_adopt_api.config import get_settings

TRIAGE_ORG_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1"
EXAMPLE_MISSION_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2"
FRONTIER_ALLIANCE_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb3"

EXPECTED_TABLES = {
    "facilitating_org",
    "fpg",
    "facilitator_fpg_coverage",
    "adopter_interest",
    "match",
    "match_attempt",
}


@pytest.fixture
async def conn() -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as connection:
        yield connection
        await connection.rollback()
    await engine.dispose()


async def _make_contact(conn: AsyncConnection) -> uuid.UUID:
    cid = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO contacts (id, party_kind, display_name) "
            "VALUES (:id, 'adopter', :name)"
        ),
        {"id": cid, "name": f"Test {cid}"},
    )
    return cid


async def _make_interest(
    conn: AsyncConnection, contact_id: uuid.UUID, people_id3: str | None = "AAA03"
) -> uuid.UUID:
    iid = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO adopter_interest (id, contact_id, people_id3) "
            "VALUES (:id, :cid, :people_id3)"
        ),
        {"id": iid, "cid": contact_id, "people_id3": people_id3},
    )
    return iid


async def test_all_six_tables_exist(conn: AsyncConnection) -> None:
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
    assert EXPECTED_TABLES.issubset(tables), (
        f"missing: {EXPECTED_TABLES - tables}"
    )


async def test_seed_data_present(conn: AsyncConnection) -> None:
    """Migration seed: 3 demo orgs, 5 FPGs, 6 coverage rows (0021 re-inserts FPGs)."""
    seed_org_ids = (
        TRIAGE_ORG_ID,
        EXAMPLE_MISSION_ID,
        FRONTIER_ALLIANCE_ID,
    )
    orgs = (
        await conn.execute(
            text(
                "SELECT COUNT(*) FROM facilitating_org "
                "WHERE id::text = ANY(:ids)"
            ),
            {"ids": list(seed_org_ids)},
        )
    ).scalar_one()
    demo_people_id3s = ("AAA01", "AAA02", "AAA03", "AAA04", "AAA05")
    fpgs = (
        await conn.execute(
            text(
                "SELECT COUNT(*) FROM fpg WHERE people_id3 = ANY(:codes)"
            ),
            {"codes": list(demo_people_id3s)},
        )
    ).scalar_one()
    cov = (
        await conn.execute(
            text(
                "SELECT COUNT(*) FROM facilitator_fpg_coverage "
                "WHERE people_id3 = ANY(:codes)"
            ),
            {"codes": list(demo_people_id3s)},
        )
    ).scalar_one()
    assert orgs == 3, f"expected 3 seed facilitating_orgs, got {orgs}"
    assert fpgs == 5, f"expected 5 demo fpgs, got {fpgs}"
    assert cov == 6, f"expected 6 demo coverage rows, got {cov}"


async def test_exactly_one_triage_org_in_seed(conn: AsyncConnection) -> None:
    """The seed loads exactly one is_triage_org=TRUE row."""
    result = await conn.execute(
        text(
            "SELECT id, name FROM facilitating_org WHERE is_triage_org = TRUE"
        )
    )
    rows = result.all()
    assert len(rows) == 1
    assert str(rows[0].id) == TRIAGE_ORG_ID
    assert rows[0].name == "Triage Queue"


async def test_unique_partial_open_match_blocks_double_recommended(
    conn: AsyncConnection,
) -> None:
    """Two 'recommended' matches on the same adopter_interest_id violate
    the partial UNIQUE index."""
    insert_match = text(
        """
        INSERT INTO match
            (id, adopter_interest_id, facilitator_org_id, status)
        VALUES
            (:id, :iid, :fid, :status)
        """
    )
    async with conn.begin():
        cid = await _make_contact(conn)
        iid = await _make_interest(conn, cid, people_id3="AAA03")
        await conn.execute(
            insert_match,
            {
                "id": uuid.uuid4(),
                "iid": iid,
                "fid": EXAMPLE_MISSION_ID,
                "status": "recommended",
            },
        )
        with pytest.raises(IntegrityError):
            await conn.execute(
                insert_match,
                {
                    "id": uuid.uuid4(),
                    "iid": iid,
                    "fid": FRONTIER_ALLIANCE_ID,
                    "status": "recommended",
                },
            )


async def test_unique_partial_open_match_allows_completed_plus_recommended(
    conn: AsyncConnection,
) -> None:
    """One 'completed' + one 'recommended' for the same interest succeeds
    because 'completed' is outside the partial-index predicate."""
    insert_match = text(
        """
        INSERT INTO match
            (id, adopter_interest_id, facilitator_org_id, status)
        VALUES
            (:id, :iid, :fid, :status)
        """
    )
    async with conn.begin():
        cid = await _make_contact(conn)
        iid = await _make_interest(conn, cid, people_id3="AAA03")
        await conn.execute(
            insert_match,
            {
                "id": uuid.uuid4(),
                "iid": iid,
                "fid": EXAMPLE_MISSION_ID,
                "status": "completed",
            },
        )
        await conn.execute(
            insert_match,
            {
                "id": uuid.uuid4(),
                "iid": iid,
                "fid": FRONTIER_ALLIANCE_ID,
                "status": "recommended",
            },
        )


async def test_capacity_committed_le_total_check(conn: AsyncConnection) -> None:
    """CHECK ck_facilitating_org_capacity_committed_le_total blocks
    capacity_committed > capacity_total."""
    async with conn.begin():
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    """
                    INSERT INTO facilitating_org
                        (id, name, capacity_total, capacity_committed)
                    VALUES
                        (:id, 'OverCapOrg', 3, 5)
                    """
                ),
                {"id": uuid.uuid4()},
            )


async def test_unique_partial_triage_org_enforced(
    conn: AsyncConnection,
) -> None:
    """Trying to mark a second org as is_triage_org=TRUE must fail
    (the partial UNIQUE index on is_triage_org WHERE is_triage_org=TRUE
    is what enforces the "exactly one triage queue" invariant)."""
    async with conn.begin():
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    """
                    INSERT INTO facilitating_org
                        (id, name, is_triage_org, capacity_total)
                    VALUES
                        (:id, 'Second Triage', TRUE, 10)
                    """
                ),
                {"id": uuid.uuid4()},
            )


async def test_cascade_delete_fpg_drops_coverage(
    conn: AsyncConnection,
) -> None:
    """Deleting an fpg row also deletes its facilitator_fpg_coverage
    rows (ON DELETE CASCADE). Wrapped in a transaction that the fixture
    rolls back so we don't disturb seed data."""
    async with conn.begin():
        await conn.execute(
            text(
                """
                INSERT INTO fpg (people_id3, name, frontier)
                VALUES ('ZZZ99', 'Throwaway FPG', TRUE)
                """
            )
        )
        await conn.execute(
            text(
                """
                INSERT INTO facilitator_fpg_coverage
                    (facilitator_org_id, people_id3)
                VALUES (:fid, 'ZZZ99')
                """
            ),
            {"fid": EXAMPLE_MISSION_ID},
        )
        before = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM facilitator_fpg_coverage "
                    "WHERE people_id3 = 'ZZZ99'"
                )
            )
        ).scalar_one()
        assert before == 1

        await conn.execute(text("DELETE FROM fpg WHERE people_id3 = 'ZZZ99'"))

        after = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM facilitator_fpg_coverage "
                    "WHERE people_id3 = 'ZZZ99'"
                )
            )
        ).scalar_one()
        assert after == 0


async def test_match_attempt_jsonb_roundtrip(conn: AsyncConnection) -> None:
    """match_attempt persists the full score_breakdown JSONB used by U6."""
    async with conn.begin():
        cid = await _make_contact(conn)
        iid = await _make_interest(conn, cid, people_id3="AAA03")
        run_id = uuid.uuid4()
        attempt_id = uuid.uuid4()

        score_breakdown = {
            "capacity_headroom": 0.85,
            "geography": 0.40,
            "language": 0.90,
            "fpg_affinity": 1.00,
            "theological": 0.50,
        }
        filter_results = {
            "hard_filter_pass": True,
            "reasons": [],
        }

        await conn.execute(
            text(
                """
                INSERT INTO match_attempt (
                    id, contact_id, adopter_interest_id, run_id,
                    candidate_facilitator_id, score, score_breakdown,
                    filter_results, rank
                ) VALUES (
                    :id, :cid, :iid, :rid, :fid, :score,
                    CAST(:breakdown AS JSONB), CAST(:filter AS JSONB), :rank
                )
                """
            ),
            {
                "id": attempt_id,
                "cid": cid,
                "iid": iid,
                "rid": run_id,
                "fid": EXAMPLE_MISSION_ID,
                "score": "0.732",
                "breakdown": json.dumps(score_breakdown),
                "filter": json.dumps(filter_results),
                "rank": 1,
            },
        )

        result = await conn.execute(
            text(
                """
                SELECT score, score_breakdown, filter_results, rank, run_id
                FROM match_attempt
                WHERE id = :id
                """
            ),
            {"id": attempt_id},
        )
        row = result.one()
        assert float(row.score) == pytest.approx(0.732)
        assert row.score_breakdown == score_breakdown
        assert row.filter_results == filter_results
        assert row.rank == 1
        assert str(row.run_id) == str(run_id)


# F1 (#52): match.is_manual_override + manual_override filter reason


async def test_match_is_manual_override_defaults_false(
    conn: AsyncConnection,
) -> None:
    """Migration 0022: a match inserted without is_manual_override defaults
    to false (existing algorithmic matches stay unflagged)."""
    async with conn.begin():
        cid = await _make_contact(conn)
        iid = await _make_interest(conn, cid, people_id3="AAA03")
        mid = uuid.uuid4()
        await conn.execute(
            text(
                """
                INSERT INTO match (id, adopter_interest_id, facilitator_org_id, status)
                VALUES (:id, :iid, :fid, 'recommended')
                """
            ),
            {"id": mid, "iid": iid, "fid": EXAMPLE_MISSION_ID},
        )
        flagged = (
            await conn.execute(
                text("SELECT is_manual_override FROM match WHERE id = :id"),
                {"id": mid},
            )
        ).scalar_one()
        assert flagged is False


def test_filter_reason_has_manual_override() -> None:
    """The manual_override audit reason resolves on the FilterReason enum."""
    from jp_adopt_api.domain.matching import FilterReason

    assert FilterReason("manual_override") is FilterReason.MANUAL_OVERRIDE
    assert FilterReason.MANUAL_OVERRIDE.value == "manual_override"
