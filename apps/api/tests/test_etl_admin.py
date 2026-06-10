"""Tests for the ETL-observability admin endpoints.

Covers:
  * Listing etl_run, migration_conflicts, etl_deleted_in_source
  * Filter behavior (table_name, mode, since, has_errors, source_system,
    conflict_type)
  * summary=true on migration-conflicts returns aggregate counts
  * Limit + total semantics (total is pre-limit, items is post-limit)
  * 403 when caller lacks staff_admin
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jp_adopt_api.config import get_settings
from jp_adopt_api.main import app
from jp_adopt_api.models import (
    EtlDeletedInSource,
    EtlRun,
    MigrationConflict,
)

os.environ.setdefault("STRICT_AUTH", "false")
os.environ.setdefault("APP_ENV", "development")
get_settings.cache_clear()


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def _auth_headers(token: str = "dev-local") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ──────────────────────────────────────────────────────────────────────────
# Seed helpers
# ──────────────────────────────────────────────────────────────────────────


_TEST_TABLE_NAME = "test_etl_admin_runs"
_TEST_SOURCE_SYSTEM = "test_etl_admin"


@pytest_asyncio.fixture
async def _seeded_etl_runs(
    session: AsyncSession,
) -> AsyncIterator[list[EtlRun]]:
    """Insert 4 etl_run rows covering the full filter matrix.

    Per-test scoping uses a synthetic ``table_name`` prefixed ``test_``
    so production-mode contacts/activity_log rows from prior tests
    don't bleed in.
    """
    now = datetime.now(UTC)
    runs = [
        EtlRun(
            id=uuid.uuid4(),
            table_name=_TEST_TABLE_NAME,
            mode="production",
            started_at=now - timedelta(hours=3),
            ended_at=now - timedelta(hours=3) + timedelta(seconds=10),
            source_max_modified_at=now - timedelta(hours=3),
            rows_in=10,
            rows_out_inserted=10,
            rows_out_updated=0,
            rows_out_skipped=0,
            rows_in_conflict=0,
            errors=0,
        ),
        EtlRun(
            id=uuid.uuid4(),
            table_name=_TEST_TABLE_NAME,
            mode="production",
            started_at=now - timedelta(hours=2),
            ended_at=now - timedelta(hours=2) + timedelta(seconds=10),
            source_max_modified_at=now - timedelta(hours=2),
            rows_in=12,
            rows_out_inserted=11,
            rows_out_updated=0,
            rows_out_skipped=1,
            rows_in_conflict=1,
            errors=1,
        ),
        EtlRun(
            id=uuid.uuid4(),
            table_name=_TEST_TABLE_NAME,
            mode="dry_run",
            started_at=now - timedelta(hours=1),
            ended_at=now - timedelta(hours=1) + timedelta(seconds=5),
            source_max_modified_at=now - timedelta(hours=1),
            rows_in=5,
            rows_out_inserted=5,
            rows_out_updated=0,
            rows_out_skipped=0,
            rows_in_conflict=0,
            errors=0,
        ),
        EtlRun(
            id=uuid.uuid4(),
            table_name=_TEST_TABLE_NAME,
            mode="production",
            started_at=now,
            ended_at=now + timedelta(seconds=10),
            source_max_modified_at=now,
            rows_in=20,
            rows_out_inserted=20,
            rows_out_updated=0,
            rows_out_skipped=0,
            rows_in_conflict=0,
            errors=0,
        ),
    ]
    session.add_all(runs)
    await session.commit()
    try:
        yield runs
    finally:
        await session.execute(
            delete(EtlRun).where(EtlRun.table_name == _TEST_TABLE_NAME)
        )
        await session.commit()


@pytest_asyncio.fixture
async def _seeded_conflicts(
    session: AsyncSession,
) -> AsyncIterator[list[MigrationConflict]]:
    now = datetime.now(UTC)
    items = [
        MigrationConflict(
            id=uuid.uuid4(),
            source_system=_TEST_SOURCE_SYSTEM,
            source_id="9001",
            table_name="contacts",
            conflict_type="duplicate_email",
            source_value={"email_normalized": "dup1@x.dev"},
            detected_at=now - timedelta(minutes=30),
        ),
        MigrationConflict(
            id=uuid.uuid4(),
            source_system=_TEST_SOURCE_SYSTEM,
            source_id="9002",
            table_name="contacts",
            conflict_type="duplicate_email",
            source_value={"email_normalized": "dup2@x.dev"},
            detected_at=now - timedelta(minutes=20),
        ),
        MigrationConflict(
            id=uuid.uuid4(),
            source_system=_TEST_SOURCE_SYSTEM,
            source_id="9003",
            table_name="contact_assignment",
            conflict_type="assignee_no_subject",
            source_value={"assigned_to": "user-9999"},
            detected_at=now - timedelta(minutes=10),
        ),
    ]
    session.add_all(items)
    await session.commit()
    try:
        yield items
    finally:
        await session.execute(
            delete(MigrationConflict).where(
                MigrationConflict.source_system == _TEST_SOURCE_SYSTEM
            )
        )
        await session.commit()


@pytest_asyncio.fixture
async def _seeded_deleted(
    session: AsyncSession,
) -> AsyncIterator[list[EtlDeletedInSource]]:
    """Seed an EtlRun (FK parent) and two EtlDeletedInSource rows.

    EtlDeletedInSource has a NOT NULL FK to etl_run.id, so the run must
    exist first.
    """
    now = datetime.now(UTC)
    run = EtlRun(
        id=uuid.uuid4(),
        table_name=_TEST_TABLE_NAME,
        mode="production",
        started_at=now,
        ended_at=now,
    )
    session.add(run)
    await session.flush()
    items = [
        EtlDeletedInSource(
            id=uuid.uuid4(),
            etl_run_id=run.id,
            table_name="contacts",
            source_system=_TEST_SOURCE_SYSTEM,
            source_id="9101",
            last_seen_at=now - timedelta(days=1),
            detected_at=now - timedelta(hours=2),
        ),
        EtlDeletedInSource(
            id=uuid.uuid4(),
            etl_run_id=run.id,
            table_name="contacts",
            source_system=_TEST_SOURCE_SYSTEM,
            source_id="9102",
            last_seen_at=now - timedelta(days=2),
            detected_at=now - timedelta(hours=1),
        ),
    ]
    session.add_all(items)
    await session.commit()
    try:
        yield items
    finally:
        await session.execute(
            delete(EtlDeletedInSource).where(
                EtlDeletedInSource.source_system == _TEST_SOURCE_SYSTEM
            )
        )
        await session.execute(
            delete(EtlRun).where(EtlRun.id == run.id)
        )
        await session.commit()


# ──────────────────────────────────────────────────────────────────────────
# etl_runs
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_etl_runs_returns_newest_first(
    client: TestClient, _seeded_etl_runs: list[EtlRun]
) -> None:
    r = client.get(
        f"/v1/admin/etl-runs?table_name={_TEST_TABLE_NAME}",
        headers=_auth_headers(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 4
    assert len(body["items"]) == 4
    timestamps = [item["started_at"] for item in body["items"]]
    assert timestamps == sorted(timestamps, reverse=True)


@pytest.mark.asyncio
async def test_list_etl_runs_filters_by_mode(
    client: TestClient, _seeded_etl_runs: list[EtlRun]
) -> None:
    r = client.get(
        f"/v1/admin/etl-runs?table_name={_TEST_TABLE_NAME}&mode=dry_run",
        headers=_auth_headers(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["mode"] == "dry_run"


@pytest.mark.asyncio
async def test_list_etl_runs_filters_by_has_errors(
    client: TestClient, _seeded_etl_runs: list[EtlRun]
) -> None:
    r = client.get(
        f"/v1/admin/etl-runs?table_name={_TEST_TABLE_NAME}&has_errors=true",
        headers=_auth_headers(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["errors"] == 1


@pytest.mark.asyncio
async def test_list_etl_runs_filters_by_since(
    client: TestClient, _seeded_etl_runs: list[EtlRun]
) -> None:
    cutoff = (datetime.now(UTC) - timedelta(hours=1, minutes=30)).isoformat()
    r = client.get(
        "/v1/admin/etl-runs",
        params={"table_name": _TEST_TABLE_NAME, "since": cutoff},
        headers=_auth_headers(),
    )
    assert r.status_code == 200
    body = r.json()
    # The first two runs are before the cutoff, so only the latter two
    # should come back.
    assert body["total"] == 2


@pytest.mark.asyncio
async def test_list_etl_runs_limit_smaller_than_total(
    client: TestClient, _seeded_etl_runs: list[EtlRun]
) -> None:
    r = client.get(
        f"/v1/admin/etl-runs?table_name={_TEST_TABLE_NAME}&limit=2",
        headers=_auth_headers(),
    )
    assert r.status_code == 200
    body = r.json()
    # total reports the full match count; items respects the limit.
    assert body["total"] == 4
    assert len(body["items"]) == 2


@pytest.mark.asyncio
async def test_list_etl_runs_requires_staff_admin(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jp_adopt_api import deps as deps_module

    async def _fake_roles(db: object, user_sub: str) -> frozenset[str]:
        return frozenset({"facilitator"})

    monkeypatch.setattr(deps_module, "load_user_roles", _fake_roles)
    r = client.get("/v1/admin/etl-runs", headers=_auth_headers())
    assert r.status_code == 403


# ──────────────────────────────────────────────────────────────────────────
# migration-conflicts
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_migration_conflicts_summary_groups_by_type(
    client: TestClient, _seeded_conflicts: list[MigrationConflict]
) -> None:
    r = client.get(
        f"/v1/admin/migration-conflicts?source_system={_TEST_SOURCE_SYSTEM}"
        "&summary=true",
        headers=_auth_headers(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    pairs = {
        (item["table_name"], item["conflict_type"]): item["count"]
        for item in body["items"]
    }
    assert pairs[("contacts", "duplicate_email")] == 2
    assert pairs[("contact_assignment", "assignee_no_subject")] == 1
    assert body["total"] == 3


@pytest.mark.asyncio
async def test_list_migration_conflicts_full_list_filters(
    client: TestClient, _seeded_conflicts: list[MigrationConflict]
) -> None:
    r = client.get(
        f"/v1/admin/migration-conflicts?source_system={_TEST_SOURCE_SYSTEM}"
        "&conflict_type=duplicate_email",
        headers=_auth_headers(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert all(
        item["conflict_type"] == "duplicate_email" for item in body["items"]
    )


@pytest.mark.asyncio
async def test_list_migration_conflicts_requires_staff_admin(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jp_adopt_api import deps as deps_module

    async def _fake_roles(db: object, user_sub: str) -> frozenset[str]:
        return frozenset({"facilitator"})

    monkeypatch.setattr(deps_module, "load_user_roles", _fake_roles)
    r = client.get(
        "/v1/admin/migration-conflicts", headers=_auth_headers()
    )
    assert r.status_code == 403


# ──────────────────────────────────────────────────────────────────────────
# etl-deleted-in-source
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_etl_deleted_in_source_returns_newest_first(
    client: TestClient,
    _seeded_deleted: list[EtlDeletedInSource],
) -> None:
    r = client.get(
        f"/v1/admin/etl-deleted-in-source?source_system={_TEST_SOURCE_SYSTEM}",
        headers=_auth_headers(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    timestamps = [item["detected_at"] for item in body["items"]]
    assert timestamps == sorted(timestamps, reverse=True)


@pytest.mark.asyncio
async def test_list_etl_deleted_in_source_requires_staff_admin(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jp_adopt_api import deps as deps_module

    async def _fake_roles(db: object, user_sub: str) -> frozenset[str]:
        return frozenset({"facilitator"})

    monkeypatch.setattr(deps_module, "load_user_roles", _fake_roles)
    r = client.get(
        "/v1/admin/etl-deleted-in-source", headers=_auth_headers()
    )
    assert r.status_code == 403


# Suppress unused-import warning for `text` — kept for future test growth
_ = text
