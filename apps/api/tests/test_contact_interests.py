"""Tests for the contact FPG-interest endpoints.

The bug: people-group interests were derived from matches, so an FPG a contact
selected but hadn't been matched on was invisible. These endpoints surface and
edit the actual adopter_interest selections.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jp_adopt_api.config import get_settings
from jp_adopt_api.main import app
from jp_adopt_api.models import AdopterInterest, Contact, Fpg

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


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer dev-local"}


async def _make_contact(session: AsyncSession) -> Contact:
    c = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name="Francis Test",
        adopter_status="engaged",
        email_normalized=f"int-{uuid.uuid4().hex[:8]}@example.com",
    )
    session.add(c)
    await session.commit()
    return c


async def _ensure_fpg(session: AsyncSession, pid: str, name: str) -> None:
    if await session.get(Fpg, pid) is None:
        session.add(Fpg(people_id3=pid, name=name, country_code="KE", frontier=True))
        await session.commit()


async def _cleanup(session: AsyncSession, contact: Contact) -> None:
    for ai in (
        await session.execute(
            AdopterInterest.__table__.select().where(
                AdopterInterest.contact_id == contact.id
            )
        )
    ).all():
        await session.execute(
            AdopterInterest.__table__.delete().where(
                AdopterInterest.id == ai.id
            )
        )
    await session.delete(await session.get(Contact, contact.id))
    await session.commit()


@pytest.mark.asyncio
async def test_get_interests_shows_unmatched_selection(
    client: TestClient, session: AsyncSession
) -> None:
    # The core fix: an FPG selection with NO match still shows.
    contact = await _make_contact(session)
    await _ensure_fpg(session, "14984", "Maasai of Kenya")
    session.add(
        AdopterInterest(
            id=uuid.uuid4(), contact_id=contact.id, people_id3="14984"
        )
    )
    await session.commit()
    try:
        r = client.get(f"/v1/contacts/{contact.id}/interests", headers=_auth())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 1
        assert body["items"][0]["people_id3"] == "14984"
        assert body["items"][0]["people_id3_name"] == "Maasai of Kenya"
    finally:
        await _cleanup(session, contact)


@pytest.mark.asyncio
async def test_add_interest(client: TestClient, session: AsyncSession) -> None:
    contact = await _make_contact(session)
    await _ensure_fpg(session, "20001", "Somali")
    try:
        r = client.post(
            f"/v1/contacts/{contact.id}/interests",
            json={"people_id3": "20001"},
            headers=_auth(),
        )
        assert r.status_code == 201, r.text
        assert r.json()["people_id3_name"] == "Somali"
        # Duplicate → 409.
        r2 = client.post(
            f"/v1/contacts/{contact.id}/interests",
            json={"people_id3": "20001"},
            headers=_auth(),
        )
        assert r2.status_code == 409, r2.text
        # Unknown FPG → 422.
        r3 = client.post(
            f"/v1/contacts/{contact.id}/interests",
            json={"people_id3": "99999"},
            headers=_auth(),
        )
        assert r3.status_code == 422, r3.text
    finally:
        await _cleanup(session, contact)


@pytest.mark.asyncio
async def test_remove_interest(
    client: TestClient, session: AsyncSession
) -> None:
    contact = await _make_contact(session)
    await _ensure_fpg(session, "20002", "Fulani")
    ai = AdopterInterest(
        id=uuid.uuid4(), contact_id=contact.id, people_id3="20002"
    )
    session.add(ai)
    await session.commit()
    try:
        r = client.delete(
            f"/v1/contacts/{contact.id}/interests/{ai.id}", headers=_auth()
        )
        assert r.status_code == 204, r.text
        # Gone now.
        g = client.get(f"/v1/contacts/{contact.id}/interests", headers=_auth())
        assert g.json()["total"] == 0
        # Removing a non-existent one → 404.
        r2 = client.delete(
            f"/v1/contacts/{contact.id}/interests/{uuid.uuid4()}",
            headers=_auth(),
        )
        assert r2.status_code == 404, r2.text
    finally:
        await _cleanup(session, contact)


@pytest.mark.asyncio
async def test_fpg_search(client: TestClient, session: AsyncSession) -> None:
    await _ensure_fpg(session, "30777", "Searchable People Group")
    r = client.get("/v1/fpgs?q=Searchable", headers=_auth())
    assert r.status_code == 200, r.text
    names = {f["people_id3"] for f in r.json()["items"]}
    assert "30777" in names
