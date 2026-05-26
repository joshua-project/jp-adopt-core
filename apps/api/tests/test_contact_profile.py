"""contact_profile (U6): 1:1 adoption-field table — constraints + cascade.

Runs against the local Postgres (see conftest). Rows tagged origin='profile_test'.
"""

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
from jp_adopt_api.models import Contact, ContactProfile

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


async def _make_contact(session: AsyncSession) -> Contact:
    c = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name="Profile Test",
        email_normalized=f"profile-{uuid.uuid4().hex[:10]}@example.com",
        origin="profile_test",
    )
    session.add(c)
    await session.commit()
    return c


@pytest_asyncio.fixture
async def cleanup(session: AsyncSession) -> AsyncIterator[None]:
    yield
    # contact_profile cascades on contact delete (FK ON DELETE CASCADE).
    await session.execute(delete(Contact).where(Contact.origin == "profile_test"))
    await session.commit()


@pytest.mark.usefixtures("cleanup")
async def test_profile_persists_with_arrays_and_enums(session: AsyncSession):
    c = await _make_contact(session)
    session.add(
        ContactProfile(
            id=uuid.uuid4(),
            contact_id=c.id,
            adopter_type="church",
            entity_size="101_500",
            mou_status="signed",
            preferred_communication="email",
            engagement_score=72,
            ministry_areas=["evangelism", "church_planting"],
            commitment_types=["prayer", "financial"],
            willing_to_facilitate=True,
        )
    )
    await session.commit()
    got = (
        await session.execute(
            select(ContactProfile).where(ContactProfile.contact_id == c.id)
        )
    ).scalar_one()
    assert got.adopter_type == "church"
    assert got.ministry_areas == ["evangelism", "church_planting"]
    assert got.engagement_score == 72


@pytest.mark.usefixtures("cleanup")
async def test_bad_enum_rejected(session: AsyncSession):
    c = await _make_contact(session)
    session.add(
        ContactProfile(id=uuid.uuid4(), contact_id=c.id, entity_size="gigantic")
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.usefixtures("cleanup")
async def test_engagement_score_range_enforced(session: AsyncSession):
    c = await _make_contact(session)
    session.add(
        ContactProfile(id=uuid.uuid4(), contact_id=c.id, engagement_score=150)
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.usefixtures("cleanup")
async def test_one_profile_per_contact(session: AsyncSession):
    c = await _make_contact(session)
    session.add(ContactProfile(id=uuid.uuid4(), contact_id=c.id))
    await session.commit()
    session.add(ContactProfile(id=uuid.uuid4(), contact_id=c.id))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.usefixtures("cleanup")
async def test_cascade_on_contact_delete(session: AsyncSession):
    c = await _make_contact(session)
    session.add(ContactProfile(id=uuid.uuid4(), contact_id=c.id))
    await session.commit()
    await session.execute(delete(Contact).where(Contact.id == c.id))
    await session.commit()
    remaining = (
        await session.execute(
            select(ContactProfile).where(ContactProfile.contact_id == c.id)
        )
    ).scalar_one_or_none()
    assert remaining is None
