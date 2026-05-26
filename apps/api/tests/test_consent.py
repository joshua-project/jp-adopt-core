"""consent (U8): MOU acceptance records — hash CHECK + cascade."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jp_adopt_api.config import get_settings
from jp_adopt_api.models import Consent, Contact

os.environ.setdefault("STRICT_AUTH", "false")
os.environ.setdefault("APP_ENV", "development")
get_settings.cache_clear()

VALID_HASH = "a" * 64


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def contact(session: AsyncSession) -> AsyncIterator[Contact]:
    c = Contact(
        id=uuid.uuid4(),
        party_kind="facilitator",
        display_name="Consent Test Org",
        email_normalized=f"consent-{uuid.uuid4().hex[:10]}@example.com",
        origin="consent_test",
    )
    session.add(c)
    await session.commit()
    yield c
    await session.execute(delete(Contact).where(Contact.origin == "consent_test"))
    await session.commit()


def _mou(contact_id, content_hash=VALID_HASH):
    return Consent(
        id=uuid.uuid4(),
        contact_id=contact_id,
        consent_type="mou",
        version="2026-05-01",
        content_hash=content_hash,
        accepted_at=datetime.now(UTC),
        conversation_id=None,
        evidence={"channel": "web", "clientName": None, "userUtterance": None},
    )


async def test_consent_persists(session: AsyncSession, contact: Contact):
    session.add(_mou(contact.id))
    await session.commit()
    got = (
        await session.execute(
            select(Consent).where(Consent.contact_id == contact.id)
        )
    ).scalar_one()
    assert got.consent_type == "mou"
    assert got.evidence["channel"] == "web"


async def test_bad_content_hash_rejected(session: AsyncSession, contact: Contact):
    session.add(_mou(contact.id, content_hash="not-a-sha256"))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_cascade_on_contact_delete(session: AsyncSession, contact: Contact):
    session.add(_mou(contact.id))
    await session.commit()
    await session.execute(delete(Contact).where(Contact.id == contact.id))
    await session.commit()
    remaining = (
        await session.execute(
            select(Consent).where(Consent.contact_id == contact.id)
        )
    ).scalar_one_or_none()
    assert remaining is None
