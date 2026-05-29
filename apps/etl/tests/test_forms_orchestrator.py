"""Integration tests for forms-etl orchestrator."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from jp_adopt_api.models import Contact, EtlRun, MigrationConflict, Outbox
from jp_adopt_api.outbox_suppression import EVENT_BULK_IMPORTED
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from jp_adopt_etl.forms_orchestrator import run_forms_etl

ETL_TEST_DATABASE_URL = os.environ.get(
    "ETL_TEST_DATABASE_URL",
    "postgresql+psycopg2://jp_adopt:jp_adopt@127.0.0.1:5434/jp_adopt",
)

pytestmark = pytest.mark.skipif(
    "ETL_TEST_DATABASE_URL_DISABLE" in os.environ,
    reason="Postgres not available",
)


def _adoption_source_row(email: str, *, created_at: datetime, row_id: str) -> dict:
    return {
        "form_type": "adoption",
        "id": row_id,
        "submission_id": f"pga_{row_id[:8]}",
        "created_at": created_at,
        "updated_at": created_at,
        "submission": {
            "email": email,
            "entity_name": "Import Entity",
            "country": "United States",
            "adopter_type": "church",
            "entity_size": "31_100",
            "preferred_communication": "email",
            "mou_accepted": False,
            "newsletter_opt_in": False,
            "ministry_areas": [],
            "partner_entity_types": [],
            "desired_partner_info": [],
        },
        "fpg_selections": [{"people_id3": "AAA01", "commitment_types": ["prayer"]}],
    }


@pytest.fixture
def mock_forms_rows():
    ts = datetime(2024, 11, 15, 10, 0, tzinfo=UTC)
    rows = [
        _adoption_source_row(
            f"forms-etl-{uuid.uuid4().hex[:6]}@example.com",
            created_at=ts,
            row_id=str(uuid.uuid4()),
        )
        for _ in range(3)
    ]
    return rows


def _delete_forms_etl_artifacts(pg_engine, mock_forms_rows) -> None:
    with pg_engine.connect() as conn:
        for row in mock_forms_rows:
            conn.execute(
                text(
                    "DELETE FROM migration_conflicts WHERE source_system = "
                    "'jp-adopt-forms' AND source_id = :sid"
                ),
                {"sid": row["id"]},
            )
            conn.execute(
                text(
                    "DELETE FROM contacts WHERE source_system = 'jp-adopt-forms' "
                    "AND source_id = :sid"
                ),
                {"sid": row["id"]},
            )
        conn.execute(text("DELETE FROM etl_run WHERE table_name = 'submissions'"))
        conn.commit()


@pytest.fixture(autouse=True)
def _cleanup(pg_engine, mock_forms_rows):
    _delete_forms_etl_artifacts(pg_engine, mock_forms_rows)
    yield
    _delete_forms_etl_artifacts(pg_engine, mock_forms_rows)


@pytest.fixture
def pg_engine():
    from sqlalchemy import create_engine

    engine = create_engine(ETL_TEST_DATABASE_URL, future=True)
    yield engine
    engine.dispose()


@pytest.mark.asyncio
async def test_forms_etl_dry_run(mock_forms_rows, pg_engine) -> None:
    with patch(
        "jp_adopt_etl.forms_orchestrator.iter_submissions",
        return_value=iter(mock_forms_rows),
    ):
        summary = await run_forms_etl(
            forms_postgres_url=ETL_TEST_DATABASE_URL,
            postgres_url=ETL_TEST_DATABASE_URL,
            mode="dry_run",
            watermark=None,
        )
    assert summary["imported"] == 3
    assert summary["mode"] == "dry_run"

    async_engine = create_async_engine(
        ETL_TEST_DATABASE_URL.replace("+psycopg2", "+asyncpg")
    )
    async with async_engine.connect() as conn:
        etl_rows = (
            await conn.execute(
                select(EtlRun).where(
                    EtlRun.table_name == "submissions", EtlRun.mode == "dry_run"
                )
            )
        ).all()
        assert len(etl_rows) == 1
        contacts = (
            await conn.execute(
                select(Contact).where(Contact.source_system == "jp-adopt-forms")
            )
        ).all()
        assert len(contacts) == 0
    await async_engine.dispose()


@pytest.mark.asyncio
async def test_forms_etl_production(mock_forms_rows, pg_engine) -> None:
    with patch(
        "jp_adopt_etl.forms_orchestrator.iter_submissions",
        return_value=iter(mock_forms_rows),
    ):
        summary = await run_forms_etl(
            forms_postgres_url=ETL_TEST_DATABASE_URL,
            postgres_url=ETL_TEST_DATABASE_URL,
            mode="production",
            watermark=None,
        )
    assert summary["imported"] == 3

    async_engine = create_async_engine(
        ETL_TEST_DATABASE_URL.replace("+psycopg2", "+asyncpg")
    )
    async with async_engine.connect() as conn:
        contacts = (
            await conn.execute(
                select(Contact).where(Contact.source_system == "jp-adopt-forms")
            )
        ).all()
        assert len(contacts) == 3
        bulk = (
            await conn.execute(
                select(Outbox).where(Outbox.event_type == EVENT_BULK_IMPORTED)
            )
        ).all()
        assert len(bulk) >= 1
    await async_engine.dispose()


@pytest.mark.asyncio
async def test_forms_etl_idempotent_rerun(mock_forms_rows, pg_engine) -> None:
    """Second production run with the same source rows skips already-imported."""
    with patch(
        "jp_adopt_etl.forms_orchestrator.iter_submissions",
        return_value=iter(mock_forms_rows),
    ):
        first = await run_forms_etl(
            forms_postgres_url=ETL_TEST_DATABASE_URL,
            postgres_url=ETL_TEST_DATABASE_URL,
            mode="production",
            watermark=None,
        )
    assert first["imported"] == 3

    with patch(
        "jp_adopt_etl.forms_orchestrator.iter_submissions",
        return_value=iter(mock_forms_rows),
    ):
        second = await run_forms_etl(
            forms_postgres_url=ETL_TEST_DATABASE_URL,
            postgres_url=ETL_TEST_DATABASE_URL,
            mode="production",
            watermark=None,
        )
    assert second["imported"] == 0
    assert second["skipped_already_imported"] == 3

    async_engine = create_async_engine(
        ETL_TEST_DATABASE_URL.replace("+psycopg2", "+asyncpg")
    )
    async with async_engine.connect() as conn:
        contacts = (
            await conn.execute(
                select(Contact).where(Contact.source_system == "jp-adopt-forms")
            )
        ).all()
        assert len(contacts) == 3
        interest_count = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM adopter_interest ai "
                    "JOIN contacts c ON c.id = ai.contact_id "
                    "WHERE c.source_system = 'jp-adopt-forms'"
                )
            )
        ).scalar_one()
        assert interest_count == 3
    await async_engine.dispose()


@pytest.mark.asyncio
async def test_mapping_failure_to_migration_conflicts(pg_engine) -> None:
    bad_row = {
        "form_type": "adoption",
        "id": str(uuid.uuid4()),
        "submission_id": "pga_bad",
        "created_at": datetime(2024, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2024, 1, 1, tzinfo=UTC),
        "submission": {"entity_name": "No Email"},
        "fpg_selections": [],
    }
    with patch(
        "jp_adopt_etl.forms_orchestrator.iter_submissions",
        return_value=iter([bad_row]),
    ):
        summary = await run_forms_etl(
            forms_postgres_url=ETL_TEST_DATABASE_URL,
            postgres_url=ETL_TEST_DATABASE_URL,
            mode="production",
            watermark=None,
        )
    assert summary["mapping_failed"] == 1

    async_engine = create_async_engine(
        ETL_TEST_DATABASE_URL.replace("+psycopg2", "+asyncpg")
    )
    async with async_engine.connect() as conn:
        conflicts = (
            await conn.execute(
                select(MigrationConflict).where(
                    MigrationConflict.source_id == bad_row["id"]
                )
            )
        ).all()
        assert len(conflicts) == 1
    await async_engine.dispose()
