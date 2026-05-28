"""Intake promotion (U10): a submission populates contact_profile, the per-FPG
adopter_interest fields, and consent records."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jp_adopt_api.config import get_settings
from jp_adopt_api.models import AdopterInterest, Consent, Contact, ContactProfile, Fpg

TEST_INTAKE_KEY = "test-intake-key-do-not-use-in-prod"
os.environ["INTAKE_API_KEYS"] = TEST_INTAKE_KEY
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


async def test_adoption_intake_persists_profile_and_consent(
    client: TestClient, session: AsyncSession
):
    email = f"u10-{uuid.uuid4().hex[:10]}@example.com"
    body = {
        "email": email,
        "display_name": "U10 Adopter Org",
        "origin": "website",
        "fpg_selections": [
            {"people_id3": "AAA01", "commitment_level": "going",
             "commitment_types": ["prayer", "financial"]},
        ],
        "profile": {
            "adopter_type": "church",
            "entity_size": "101_500",
            "ministry_areas": ["evangelism"],
            "engagement_score": 40,
            "referral_source": "a partner",  # readonly-after, but set at intake
            "campaign": "spring-2026",
            "this_is_unknown": "ignored",  # extra=ignore on profile
        },
        "consents": [
            {
                "consent_type": "mou",
                "version": "2026-05-01",
                "content_hash": "b" * 64,
                "accepted_at": "2026-05-26T12:00:00Z",
                "conversation_id": None,
                "evidence": {"channel": "web"},
            }
        ],
    }
    try:
        r = client.post(
            "/v1/intake/adoption",
            json=body,
            headers={
                "Authorization": f"Bearer {TEST_INTAKE_KEY}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        assert r.status_code == 201, r.text
        contact_id = uuid.UUID(r.json()["data"]["contactId"])

        prof = (
            await session.execute(
                select(ContactProfile).where(ContactProfile.contact_id == contact_id)
            )
        ).scalar_one()
        assert prof.adopter_type == "church"
        assert prof.entity_size == "101_500"
        assert prof.ministry_areas == ["evangelism"]
        assert prof.referral_source == "a partner"

        interest = (
            await session.execute(
                select(AdopterInterest).where(
                    AdopterInterest.contact_id == contact_id
                )
            )
        ).scalar_one()
        assert interest.commitment_types == ["prayer", "financial"]

        consent = (
            await session.execute(
                select(Consent).where(Consent.contact_id == contact_id)
            )
        ).scalar_one()
        assert consent.consent_type == "mou"
        assert consent.evidence == {"channel": "web"}
    finally:
        await session.execute(
            delete(Contact).where(Contact.email_normalized == email)
        )
        await session.commit()


async def test_adoption_intake_stores_people_id3_directly(
    client: TestClient, session: AsyncSession
):
    email = f"pid-{uuid.uuid4().hex[:10]}@example.com"
    people_id3 = "9990001"
    session.add(
        Fpg(
            people_id3=people_id3,
            name="Test PID group",
            country_code="US",
            frontier=True,
        )
    )
    await session.commit()
    body = {
        "email": email,
        "display_name": "PID Adopter",
        "origin": "website",
        "fpg_selections": [{"people_id3": int(people_id3), "commitment_level": "going"}],
    }
    try:
        r = client.post(
            "/v1/intake/adoption",
            json=body,
            headers={
                "Authorization": f"Bearer {TEST_INTAKE_KEY}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        assert r.status_code == 201, r.text
        contact_id = uuid.UUID(r.json()["data"]["contactId"])
        interest = (
            await session.execute(
                select(AdopterInterest).where(AdopterInterest.contact_id == contact_id)
            )
        ).scalar_one()
        assert interest.people_id3 == people_id3
    finally:
        await session.execute(delete(Contact).where(Contact.email_normalized == email))
        await session.execute(delete(Fpg).where(Fpg.people_id3 == people_id3))
        await session.commit()


async def test_facilitation_intake_creates_per_fpg_interests(
    client: TestClient, session: AsyncSession
):
    # Facilitators pick FPGs by people_id3; per-FPG services land on adopter_interest.
    email = f"facpid-{uuid.uuid4().hex[:10]}@example.com"
    people_id3 = "9990002"
    session.add(
        Fpg(
            people_id3=people_id3,
            name="Facilitation PID group",
            country_code="US",
            frontier=True,
        )
    )
    await session.commit()
    body = {
        "email": email,
        "display_name": "Helper Org",
        "origin": "website",
        "organization_name": "Helper Org",
        "fpg_selections": [
            {
                "people_id3": int(people_id3),
                "engagement_status": "ready",
                "facilitation_services": ["coaching"],
                "network_services": ["intro"],
            }
        ],
    }
    try:
        r = client.post(
            "/v1/intake/facilitation",
            json=body,
            headers={
                "Authorization": f"Bearer {TEST_INTAKE_KEY}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        assert r.status_code == 201, r.text
        assert len(r.json()["data"]["interestIds"]) == 1
        contact_id = uuid.UUID(r.json()["data"]["contactId"])
        interest = (
            await session.execute(
                select(AdopterInterest).where(AdopterInterest.contact_id == contact_id)
            )
        ).scalar_one()
        assert interest.people_id3 == people_id3
        assert interest.engagement_status == "ready"
        assert interest.facilitation_services == ["coaching"]
        assert interest.network_services == ["intro"]
    finally:
        await session.execute(delete(Contact).where(Contact.email_normalized == email))
        await session.execute(delete(Fpg).where(Fpg.people_id3 == people_id3))
        await session.commit()
