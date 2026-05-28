"""ContactRead/ContactPatch profile (U9): read, edit, and the guard rails.

Patching the profile must NOT bump Contact.version, status stays forbidden,
readonly + bad-enum profile fields are 422.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jp_adopt_api.config import get_settings
from jp_adopt_api.models import Contact

os.environ.setdefault("STRICT_AUTH", "false")
os.environ.setdefault("APP_ENV", "development")
get_settings.cache_clear()

AUTH = {"Authorization": "Bearer dev-local"}


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def contact_id(session: AsyncSession) -> AsyncIterator[str]:
    c = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name="Profile API Test",
        adopter_status="new",
        email_normalized=f"profapi-{uuid.uuid4().hex[:10]}@example.com",
        origin="profile_api_test",
    )
    session.add(c)
    await session.commit()
    yield str(c.id)
    await session.execute(delete(Contact).where(Contact.origin == "profile_api_test"))
    await session.commit()


def test_patch_profile_persists_and_reads_back(client: TestClient, contact_id: str):
    before = client.get(f"/v1/contacts/{contact_id}", headers=AUTH).json()
    assert before["profile"] is None
    v0 = before["version"]

    r = client.patch(
        f"/v1/contacts/{contact_id}",
        headers=AUTH,
        json={"profile": {"entity_size": "101_500", "mou_status": "signed",
                           "ministry_areas": ["evangelism"], "engagement_score": 60}},
    )
    assert r.status_code == 200, r.text
    prof = r.json()["profile"]
    assert prof["entity_size"] == "101_500"
    assert prof["ministry_areas"] == ["evangelism"]
    # Profile edit must NOT bump Contact.version.
    assert r.json()["version"] == v0

    again = client.get(f"/v1/contacts/{contact_id}", headers=AUTH).json()
    assert again["profile"]["mou_status"] == "signed"


def test_status_field_still_forbidden(client: TestClient, contact_id: str):
    r = client.patch(
        f"/v1/contacts/{contact_id}", headers=AUTH, json={"adopter_status": "matched"}
    )
    assert r.status_code == 422


def test_readonly_profile_field_rejected(client: TestClient, contact_id: str):
    r = client.patch(
        f"/v1/contacts/{contact_id}",
        headers=AUTH,
        json={"profile": {"referral_source": "a friend"}},
    )
    assert r.status_code == 422


def test_bad_enum_rejected_at_schema(client: TestClient, contact_id: str):
    r = client.patch(
        f"/v1/contacts/{contact_id}",
        headers=AUTH,
        json={"profile": {"entity_size": "ginormous"}},
    )
    assert r.status_code == 422


def test_empty_profile_patch_does_not_create_row(client: TestClient, contact_id: str):
    # Copilot review: an empty {profile:{}} must NOT materialize a profile row.
    r = client.patch(f"/v1/contacts/{contact_id}", headers=AUTH, json={"profile": {}})
    assert r.status_code == 200, r.text
    assert r.json()["profile"] is None
    again = client.get(f"/v1/contacts/{contact_id}", headers=AUTH).json()
    assert again["profile"] is None


def test_engagement_score_range_rejected(client: TestClient, contact_id: str):
    r = client.patch(
        f"/v1/contacts/{contact_id}",
        headers=AUTH,
        json={"profile": {"engagement_score": 250}},
    )
    assert r.status_code == 422
