"""Drip enrollments read endpoint (#55 slice): GET /v1/contacts/{id}/enrollments."""

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
from jp_adopt_api.models import Campaign, Contact, Enrollment

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


async def test_enrollments_returns_campaign_and_state(
    client: TestClient, session: AsyncSession
):
    campaign = Campaign(id=uuid.uuid4(), name="Enroll Test Drip", status="active")
    contact = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name="Enroll Test",
        email_normalized=f"enroll-{uuid.uuid4().hex[:10]}@example.com",
        origin="enroll_test",
    )
    session.add_all([campaign, contact])
    await session.flush()
    session.add(
        Enrollment(
            id=uuid.uuid4(),
            campaign_id=campaign.id,
            contact_id=contact.id,
            state="active",
            current_step_position=1,
        )
    )
    await session.commit()
    try:
        r = client.get(f"/v1/contacts/{contact.id}/enrollments", headers=AUTH)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 1
        assert body["items"][0]["campaign_name"] == "Enroll Test Drip"
        assert body["items"][0]["state"] == "active"
        assert body["items"][0]["current_step_position"] == 1
    finally:
        # Deleting the contact cascades the enrollment; then the campaign is free.
        await session.execute(delete(Contact).where(Contact.id == contact.id))
        await session.execute(delete(Campaign).where(Campaign.id == campaign.id))
        await session.commit()
