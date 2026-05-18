"""Magic-link side-car: happy path + the failure modes the plan calls out
(expired, already-claimed, account-resolution-conflict, rate-limit,
anti-enumeration).

Service-layer tests use a per-test async engine (avoids the cross-event-loop
issue triggered by sharing the cached app-engine with ``asyncio.run``).
Router tests use the FastAPI TestClient and clean up via a parallel helper
engine.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import jwt
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jp_adopt_api.auth_magic import (
    MAGIC_LINK_RATE_LIMIT_PER_HOUR,
    MAGIC_LINK_TTL_SECONDS,
    AccountResolutionConflictError,
    MagicLinkAlreadyClaimedError,
    MagicLinkExpiredError,
    MagicLinkInvalidError,
    RateLimitedError,
    claim_magic_link,
    decode_magic_link_token,
    generate_token,
    normalize_email,
    request_magic_link,
)
from jp_adopt_api.config import Settings, get_settings
from jp_adopt_api.main import app
from jp_adopt_api.models import (
    IdentityLink,
    MagicLinkRateLimit,
    MagicLinkToken,
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Per-test async session backed by a fresh engine."""
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _clean(session: AsyncSession, email_normalized: str) -> None:
    await session.execute(
        delete(MagicLinkRateLimit).where(
            MagicLinkRateLimit.email_normalized == email_normalized
        )
    )
    await session.execute(
        delete(MagicLinkToken).where(
            MagicLinkToken.email_normalized == email_normalized
        )
    )
    await session.execute(
        delete(IdentityLink).where(
            IdentityLink.email_normalized == email_normalized
        )
    )
    await session.commit()


# --- pure-function tests ------------------------------------------------------


def test_normalize_email_lowercases_strips_trailing_dot() -> None:
    assert normalize_email("  Foo@Example.com.  ") == "foo@example.com"


def test_generate_token_returns_url_safe_and_hash() -> None:
    s = Settings()
    raw, h = generate_token(s.magic_link_signing_key)
    assert len(raw) >= 32
    assert all(c.isalnum() or c in "-_" for c in raw)
    assert len(h) == 64  # sha256 hex
    raw2, h2 = generate_token(s.magic_link_signing_key)
    assert raw != raw2
    assert h != h2


# --- service-layer tests ------------------------------------------------------


@pytest.mark.asyncio
async def test_request_happy_path_persists_token_and_rate_limit(
    session: AsyncSession,
) -> None:
    settings = Settings()
    email = f"happy-{uuid.uuid4().hex[:8]}@example.test"
    await _clean(session, normalize_email(email))
    try:
        result, _raw, _norm = await request_magic_link(
            session, email=email, ip="127.0.0.1", settings=settings
        )
        await session.commit()
        assert result.ok is True

        tokens = (
            await session.execute(
                select(MagicLinkToken).where(
                    MagicLinkToken.email_normalized == normalize_email(email)
                )
            )
        ).scalars().all()
        assert len(tokens) == 1
        assert tokens[0].claimed_at is None
        assert tokens[0].expires_at - tokens[0].requested_at > timedelta(
            seconds=MAGIC_LINK_TTL_SECONDS - 5
        )
    finally:
        await _clean(session, normalize_email(email))


@pytest.mark.asyncio
async def test_request_rate_limit_after_n_requests(session: AsyncSession) -> None:
    settings = Settings()
    email = f"rl-{uuid.uuid4().hex[:8]}@example.test"
    await _clean(session, normalize_email(email))
    try:
        for _ in range(MAGIC_LINK_RATE_LIMIT_PER_HOUR):
            await request_magic_link(
                session, email=email, ip="127.0.0.1", settings=settings
            )
        await session.commit()

        with pytest.raises(RateLimitedError):
            await request_magic_link(
                session, email=email, ip="127.0.0.1", settings=settings
            )
        await session.rollback()
    finally:
        await _clean(session, normalize_email(email))


@pytest.mark.asyncio
async def test_claim_happy_path_mints_jwt_and_creates_identity_link(
    session: AsyncSession,
) -> None:
    settings = Settings()
    email = f"happy-claim-{uuid.uuid4().hex[:8]}@example.test"
    await _clean(session, normalize_email(email))
    try:
        raw, token_hash = generate_token(settings.magic_link_signing_key)
        now = datetime.now(UTC)
        session.add(
            MagicLinkToken(
                id=uuid.uuid4(),
                email=email,
                email_normalized=normalize_email(email),
                token_hash=token_hash,
                expires_at=now + timedelta(seconds=MAGIC_LINK_TTL_SECONDS),
                requested_ip="127.0.0.1",
            )
        )
        await session.commit()

        claim = await claim_magic_link(
            session,
            raw_token=raw,
            click_ip="127.0.0.1",
            user_agent="pytest",
            settings=settings,
        )
        await session.commit()

        assert claim.token_type == "Bearer"
        decoded = decode_magic_link_token(claim.access_token, settings)
        assert decoded.tid == "magic_link"
        assert decoded.email == email

        link = (
            await session.execute(
                select(IdentityLink).where(
                    IdentityLink.email_normalized == normalize_email(email)
                )
            )
        ).scalars().first()
        assert link is not None
        assert link.idp_name == "magic_link"
    finally:
        await _clean(session, normalize_email(email))


@pytest.mark.asyncio
async def test_claim_second_time_raises_already_claimed(
    session: AsyncSession,
) -> None:
    settings = Settings()
    email = f"twice-{uuid.uuid4().hex[:8]}@example.test"
    await _clean(session, normalize_email(email))
    try:
        raw, token_hash = generate_token(settings.magic_link_signing_key)
        now = datetime.now(UTC)
        session.add(
            MagicLinkToken(
                id=uuid.uuid4(),
                email=email,
                email_normalized=normalize_email(email),
                token_hash=token_hash,
                expires_at=now + timedelta(seconds=MAGIC_LINK_TTL_SECONDS),
            )
        )
        await session.commit()

        await claim_magic_link(
            session, raw_token=raw, click_ip=None, user_agent=None, settings=settings
        )
        await session.commit()

        with pytest.raises(MagicLinkAlreadyClaimedError):
            await claim_magic_link(
                session,
                raw_token=raw,
                click_ip=None,
                user_agent=None,
                settings=settings,
            )
        await session.rollback()
    finally:
        await _clean(session, normalize_email(email))


@pytest.mark.asyncio
async def test_claim_expired_raises_expired(session: AsyncSession) -> None:
    settings = Settings()
    email = f"expired-{uuid.uuid4().hex[:8]}@example.test"
    await _clean(session, normalize_email(email))
    try:
        raw, token_hash = generate_token(settings.magic_link_signing_key)
        now = datetime.now(UTC)
        session.add(
            MagicLinkToken(
                id=uuid.uuid4(),
                email=email,
                email_normalized=normalize_email(email),
                token_hash=token_hash,
                expires_at=now - timedelta(seconds=1),
            )
        )
        await session.commit()

        with pytest.raises(MagicLinkExpiredError):
            await claim_magic_link(
                session,
                raw_token=raw,
                click_ip=None,
                user_agent=None,
                settings=settings,
            )
        await session.rollback()
    finally:
        await _clean(session, normalize_email(email))


@pytest.mark.asyncio
async def test_claim_unknown_token_raises_invalid(session: AsyncSession) -> None:
    settings = Settings()
    with pytest.raises(MagicLinkInvalidError):
        await claim_magic_link(
            session,
            raw_token="totally-not-a-real-token",
            click_ip=None,
            user_agent=None,
            settings=settings,
        )


@pytest.mark.asyncio
async def test_claim_account_resolution_conflict_when_b2c_exists(
    session: AsyncSession,
) -> None:
    settings = Settings()
    email = f"conflict-{uuid.uuid4().hex[:8]}@example.test"
    await _clean(session, normalize_email(email))
    try:
        session.add(
            IdentityLink(
                id=uuid.uuid4(),
                b2c_subject_id=f"existing-b2c-{uuid.uuid4().hex}",
                email=email,
                email_normalized=normalize_email(email),
                idp_name="b2c",
            )
        )
        raw, token_hash = generate_token(settings.magic_link_signing_key)
        session.add(
            MagicLinkToken(
                id=uuid.uuid4(),
                email=email,
                email_normalized=normalize_email(email),
                token_hash=token_hash,
                expires_at=datetime.now(UTC)
                + timedelta(seconds=MAGIC_LINK_TTL_SECONDS),
            )
        )
        await session.commit()

        with pytest.raises(AccountResolutionConflictError):
            await claim_magic_link(
                session,
                raw_token=raw,
                click_ip=None,
                user_agent=None,
                settings=settings,
            )
        await session.rollback()
    finally:
        await _clean(session, normalize_email(email))


def test_decode_magic_link_rejects_wrong_signing_key() -> None:
    settings = Settings()
    other = Settings(magic_link_signing_key="x" * 40)
    payload = {
        "iss": settings.magic_link_issuer,
        "sub": str(uuid.uuid4()),
        "email": "x@example.test",
        "idp": "magic_link",
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int(datetime.now(UTC).timestamp()) + 60,
    }
    token = jwt.encode(payload, settings.magic_link_signing_key, algorithm="HS256")
    with pytest.raises(jwt.InvalidSignatureError):
        decode_magic_link_token(token, other)


# --- router integration tests -------------------------------------------------


@pytest_asyncio.fixture
async def cleaner() -> AsyncIterator[AsyncSession]:
    """Companion session used inside synchronous router tests to seed/clean."""
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def test_router_request_returns_202_and_anti_enumeration_message() -> None:
    _reset_app_engine()
    # Unique email per test run so previous-test rate-limit rows do not poison this.
    email = f"unknown-anti-enum-{uuid.uuid4().hex[:8]}@example.test"
    with TestClient(app) as client:
        r = client.post(
            "/v1/auth/magic-link/request",
            json={"email": email},
        )
    assert r.status_code == 202
    assert r.json() == {
        "ok": True,
        "message": "If we have your email, we sent a link.",
    }


def _reset_app_engine() -> None:
    """The app-level engine in jp_adopt_api.db is cached as a module global.
    Tests that combine TestClient(app) with async fixtures need to reset it
    between tests so the engine is created inside the current event loop.
    """
    import jp_adopt_api.db as appdb

    appdb._engine = None
    appdb._session_factory = None


def test_router_request_429_when_rate_limited() -> None:
    _reset_app_engine()
    email = f"rl-router-{uuid.uuid4().hex[:8]}@example.test"
    with TestClient(app) as client:
        for _ in range(MAGIC_LINK_RATE_LIMIT_PER_HOUR):
            assert client.post(
                "/v1/auth/magic-link/request", json={"email": email}
            ).status_code == 202
        r = client.post("/v1/auth/magic-link/request", json={"email": email})
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "rate_limited"


def test_router_claim_invalid_token_returns_400() -> None:
    _reset_app_engine()
    with TestClient(app) as client:
        r = client.post(
            "/v1/auth/magic-link/claim",
            json={"token": "not-a-real-token"},
        )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_token"


def test_router_claim_returns_410_expired() -> None:
    """Seed an expired token row by posting an already-expired row through
    a direct asyncpg-driven engine before exercising the router."""
    _reset_app_engine()
    settings = get_settings()
    email = f"router-exp-{uuid.uuid4().hex[:8]}@example.test"
    raw, token_hash = generate_token(settings.magic_link_signing_key)

    import asyncio

    async def _seed_and_call() -> tuple[int, dict]:
        engine = create_async_engine(settings.database_url)
        factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as s:
            s.add(
                MagicLinkToken(
                    id=uuid.uuid4(),
                    email=email,
                    email_normalized=normalize_email(email),
                    token_hash=token_hash,
                    expires_at=datetime.now(UTC) - timedelta(seconds=1),
                )
            )
            await s.commit()
        await engine.dispose()
        # Reset app engine again before the TestClient creates a new one.
        _reset_app_engine()
        with TestClient(app) as client:
            r = client.post("/v1/auth/magic-link/claim", json={"token": raw})
        return r.status_code, r.json()

    status_code, body = asyncio.new_event_loop().run_until_complete(_seed_and_call())
    assert status_code == 410
    assert body["detail"]["code"] == "expired"
