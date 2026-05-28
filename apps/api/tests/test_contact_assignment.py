"""Staff assignment (U13): assign / reassign / unassign + assigned_to in read."""

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
        display_name="Assign Test",
        adopter_status="new",
        email_normalized=f"assign-{uuid.uuid4().hex[:10]}@example.com",
        origin="assign_test",
    )
    session.add(c)
    await session.commit()
    yield str(c.id)
    await session.execute(delete(Contact).where(Contact.origin == "assign_test"))
    await session.commit()


def test_assign_to_caller_then_reassign_then_unassign(
    client: TestClient, contact_id: str
):
    base = f"/v1/contacts/{contact_id}"
    assert client.get(base, headers=AUTH).json()["assigned_to"] is None

    # Assign to caller (no body subject) → dev-local.
    r = client.put(f"{base}/assignment", headers=AUTH, json={})
    assert r.status_code == 200, r.text
    assert r.json()["assigned_to"] == "dev-local"

    # Reassign to someone else (replaces, 1:1).
    r = client.put(
        f"{base}/assignment", headers=AUTH, json={"user_subject_id": "staff-amy"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["assigned_to"] == "staff-amy"
    assert client.get(base, headers=AUTH).json()["assigned_to"] == "staff-amy"

    # Unassign.
    r = client.delete(f"{base}/assignment", headers=AUTH)
    assert r.status_code == 204, r.text
    assert client.get(base, headers=AUTH).json()["assigned_to"] is None


def test_assign_unknown_contact_404(client: TestClient):
    r = client.put(f"/v1/contacts/{uuid.uuid4()}/assignment", headers=AUTH, json={})
    assert r.status_code == 404
