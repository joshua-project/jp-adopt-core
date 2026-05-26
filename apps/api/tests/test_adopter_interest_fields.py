"""adopter_interest per-FPG fields (U7): arrays + engagement_status CHECK."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

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
from jp_adopt_api.models import AdopterInterest, Contact

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


@pytest_asyncio.fixture
async def contact(session: AsyncSession) -> AsyncIterator[Contact]:
    c = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name="Interest Field Test",
        email_normalized=f"interest-{uuid.uuid4().hex[:10]}@example.com",
        origin="interest_test",
    )
    session.add(c)
    await session.commit()
    yield c
    # adopter_interest cascades on contact delete.
    await session.execute(delete(Contact).where(Contact.origin == "interest_test"))
    await session.commit()


async def test_per_fpg_fields_persist(session: AsyncSession, contact: Contact):
    session.add(
        AdopterInterest(
            id=uuid.uuid4(),
            contact_id=contact.id,
            rop3=None,
            commitment_types=["prayer", "financial"],
            engagement_status="ready",
            facilitation_services=["prayer_updates", "training"],
            network_services=["info_sharing"],
        )
    )
    await session.commit()
    got = (
        await session.execute(
            select(AdopterInterest).where(AdopterInterest.contact_id == contact.id)
        )
    ).scalar_one()
    assert got.commitment_types == ["prayer", "financial"]
    assert got.engagement_status == "ready"
    assert got.facilitation_services == ["prayer_updates", "training"]


async def test_bad_engagement_status_rejected(session: AsyncSession, contact: Contact):
    session.add(
        AdopterInterest(
            id=uuid.uuid4(), contact_id=contact.id, engagement_status="enthusiastic"
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()
