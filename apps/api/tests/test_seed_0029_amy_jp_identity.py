"""0029 seeds Amy Banta's joshuaproject.net identity (role + profile)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jp_adopt_api.config import get_settings

_OID = "77fb39e1-3acd-4012-bd8d-2a2a34534dc1"
_OLD_OID = "c3c8a516-4d53-4336-a1c1-ceb56fbb9d7c"  # amy.banta@globalspecifics.com


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_amy_jp_identity_seeded(session: AsyncSession) -> None:
    roles = (
        await session.execute(
            sa.text(
                "SELECT r.name FROM user_roles ur "
                "JOIN roles r ON r.id = ur.role_id "
                "WHERE ur.user_subject_id = :oid"
            ),
            {"oid": _OID},
        )
    ).scalars().all()
    assert "staff_admin" in roles

    email = (
        await session.execute(
            sa.text(
                "SELECT email_normalized FROM staff_profile "
                "WHERE b2c_subject_id = :oid"
            ),
            {"oid": _OID},
        )
    ).scalar_one_or_none()
    assert email == "amy.banta@joshuaproject.net"


@pytest.mark.asyncio
async def test_old_globalspecifics_profile_opted_out_of_digest(
    session: AsyncSession,
) -> None:
    """Amy's prior globalspecifics.com profile is digest-opted-out so she
    isn't emailed at both mailboxes."""
    opt_in = (
        await session.execute(
            sa.text(
                "SELECT digest_opt_in FROM staff_profile "
                "WHERE b2c_subject_id = :oid"
            ),
            {"oid": _OLD_OID},
        )
    ).scalar_one_or_none()
    assert opt_in is False
