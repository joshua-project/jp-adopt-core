"""fpg sync (U12): JP-API PGIC rows -> one fpg row per ROP3, upserted."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jp_adopt_api.config import get_settings
from jp_adopt_api.models import Fpg
from jp_adopt_api.scripts.sync_fpg import normalize_rows, upsert_fpg

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


def test_normalize_dedups_by_rop3_picking_max_population():
    raw = [
        {
            "ROP3": "100425",
            "PeopleID3": 10375,
            "Ctry": "Sri Lanka",
            "ISO3": "lka",
            "Population": 5600,
            "PeopNameAcrossCountries": "Memon",
            "PeopNameInCountry": "Memon (Sri Lanka)",
        },
        {
            "ROP3": "100425",
            "PeopleID3": 10375,
            "Ctry": "Pakistan",
            "ISO3": "pak",
            "Population": 6200,
            "PeopNameAcrossCountries": "Memon",
            "PeopNameInCountry": "Memon (Pakistan)",
        },
    ]
    rows = normalize_rows(raw)
    assert len(rows) == 1
    row = rows[0]
    assert row["rop3"] == "100425"
    assert row["people_id3"] == "10375"  # numeric -> string
    assert row["country_code"] == "PAK"  # higher-population country wins, upper
    assert row["name"] == "Memon"  # across-countries name
    assert row["frontier"] is True


def test_normalize_skips_rows_missing_identifiers():
    raw = [
        {"PeopleID3": 999, "Population": 10},  # no ROP3
        {"ROP3": "200111", "Population": 10},  # no PeopleID3
        {"ROP3": "200222", "PeopleID3": 12345, "ISO3": "IND", "Population": 1},
    ]
    rows = normalize_rows(raw)
    assert [r["rop3"] for r in rows] == ["200222"]


def test_normalize_falls_back_to_in_country_name():
    raw = [{"ROP3": "300333", "PeopleID3": 1, "PeopNameInCountry": "Only In Ctry"}]
    rows = normalize_rows(raw)
    assert rows[0]["name"] == "Only In Ctry"
    assert rows[0]["country_code"] is None


async def test_upsert_inserts_then_updates_on_conflict(session: AsyncSession):
    rop3 = "ZSYNC1"
    try:
        n = await upsert_fpg(
            session,
            [
                {
                    "rop3": rop3,
                    "people_id3": "111",
                    "name": "Sync Test",
                    "country_code": "IND",
                    "frontier": True,
                }
            ],
        )
        assert n == 1
        row = (
            await session.execute(select(Fpg).where(Fpg.rop3 == rop3))
        ).scalar_one()
        assert row.people_id3 == "111"

        # Re-running the sync with a changed people_id3 updates in place.
        await upsert_fpg(
            session,
            [
                {
                    "rop3": rop3,
                    "people_id3": "222",
                    "name": "Sync Test v2",
                    "country_code": "PAK",
                    "frontier": True,
                }
            ],
        )
        session.expire_all()
        row = (
            await session.execute(select(Fpg).where(Fpg.rop3 == rop3))
        ).scalar_one()
        assert row.people_id3 == "222"
        assert row.name == "Sync Test v2"
        assert row.country_code == "PAK"
    finally:
        await session.execute(delete(Fpg).where(Fpg.rop3 == rop3))
        await session.commit()
