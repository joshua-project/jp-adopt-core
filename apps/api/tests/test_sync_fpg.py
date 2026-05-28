"""fpg sync: forms export -> core fpg upsert."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jp_adopt_api.config import get_settings
from jp_adopt_api.models import Fpg
from jp_adopt_api.scripts.sync_fpg import (
    fetch_from_forms_export,
    normalize_rows,
    sync,
    upsert_fpg,
)

os.environ.setdefault("STRICT_AUTH", "false")
os.environ.setdefault("APP_ENV", "development")
get_settings.cache_clear()


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def test_normalize_rows_maps_export_shape():
    rows = normalize_rows(
        [
            {
                "people_id3": "10375",
                "name": "Arab, general",
                "country_code": "pak",
                "frontier": True,
            },
            {"name": "missing id"},
        ]
    )
    assert len(rows) == 1
    assert rows[0] == {
        "people_id3": "10375",
        "name": "Arab, general",
        "country_code": "PAK",
        "frontier": True,
    }


async def test_upsert_inserts_then_updates_on_conflict(session: AsyncSession):
    people_id3 = "10375"
    try:
        n = await upsert_fpg(
            session,
            [
                {
                    "people_id3": people_id3,
                    "name": "Sync Test",
                    "country_code": "IND",
                    "frontier": True,
                }
            ],
        )
        assert n == 1
        row = (
            await session.execute(select(Fpg).where(Fpg.people_id3 == people_id3))
        ).scalar_one()
        assert row.name == "Sync Test"

        await upsert_fpg(
            session,
            [
                {
                    "people_id3": people_id3,
                    "name": "Sync Test v2",
                    "country_code": "PAK",
                    "frontier": True,
                }
            ],
        )
        session.expire_all()
        row = (
            await session.execute(select(Fpg).where(Fpg.people_id3 == people_id3))
        ).scalar_one()
        assert row.name == "Sync Test v2"
        assert row.country_code == "PAK"
    finally:
        await session.execute(delete(Fpg).where(Fpg.people_id3 == people_id3))
        await session.commit()


async def test_sync_end_to_end_with_mocked_forms_export(session: AsyncSession):
    people_id3 = "99901"
    export_rows = [
        {
            "people_id3": people_id3,
            "name": "Mock Group",
            "country_code": "USA",
            "frontier": True,
        }
    ]

    async def fake_fetch(_url: str, _key: str) -> list[dict]:
        return export_rows

    get_settings.cache_clear()
    with (
        patch.dict(
            os.environ,
            {
                "FORMS_EXPORT_URL": "http://forms.test",
                "FORMS_EXPORT_API_KEY": "test-key",
            },
        ),
        patch(
            "jp_adopt_api.scripts.sync_fpg.fetch_from_forms_export",
            new=AsyncMock(side_effect=fake_fetch),
        ),
    ):
        get_settings.cache_clear()
        assert await sync() == 0

    try:
        row = (
            await session.execute(select(Fpg).where(Fpg.people_id3 == people_id3))
        ).scalar_one()
        assert row.name == "Mock Group"
        assert row.country_code == "USA"
    finally:
        await session.execute(delete(Fpg).where(Fpg.people_id3 == people_id3))
        await session.commit()


async def test_fetch_from_forms_export_parses_envelope():
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "ok": True,
                "data": {
                    "data": [{"people_id3": "1", "name": "A", "frontier": True}],
                    "count": 1,
                },
            }

    class FakeClient:
        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, headers: dict[str, str]) -> FakeResponse:
            assert "frontier_only=true" in url
            assert headers["Authorization"] == "Bearer secret"
            return FakeResponse()

    with patch("jp_adopt_api.scripts.sync_fpg.httpx.AsyncClient", return_value=FakeClient()):
        rows = await fetch_from_forms_export("http://forms.test", "secret")
    assert rows[0]["people_id3"] == "1"
