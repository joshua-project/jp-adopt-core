"""Entra direct side-car: enforce ``partner_tenants`` allowlist and the
basic JWT verification contract.

Spinning up a real JWKS endpoint just for tests is overkill for week 1 —
we exercise the most-important property (unprovisioned tid → 403) plus the
RS256-only header check via direct calls to ``decode_entra_direct_token``.
The provisioned-tenant happy path is covered by a stubbed JWKS client
fixture: we monkeypatch ``get_entra_jwks_client`` to return a key we control,
sign a token with the matching private key, and verify the full round-trip.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator

import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jp_adopt_api.auth_entra import (
    TenantNotProvisionedError,
    decode_entra_direct_token,
)
from jp_adopt_api.config import Settings, get_settings
from jp_adopt_api.models import PartnerTenant


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def _gen_rsa_keypair() -> tuple[str, str]:
    """Return (private_pem, public_pem) for tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return priv, pub


@pytest.mark.asyncio
async def test_unprovisioned_tenant_raises_tenant_not_provisioned(
    session: AsyncSession,
) -> None:
    settings = Settings(entra_direct_audience="api://jp-adopt-core")
    tid = str(uuid.uuid4())
    # Hand-craft a token whose tid is NOT in partner_tenants.
    priv, _ = _gen_rsa_keypair()
    payload = {
        "iss": f"https://login.microsoftonline.com/{tid}/v2.0",
        "tid": tid,
        "sub": "subject-123",
        "oid": "oid-123",
        "aud": settings.entra_direct_audience,
        "iat": int(time.time()),
        "exp": int(time.time()) + 60,
    }
    token = jwt.encode(payload, priv, algorithm="RS256")
    with pytest.raises(TenantNotProvisionedError):
        await decode_entra_direct_token(session, token, settings)


@pytest.mark.asyncio
async def test_provisioned_tenant_with_valid_token_returns_auth_user(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(entra_direct_audience="api://jp-adopt-core")
    tid = str(uuid.uuid4())
    priv, pub = _gen_rsa_keypair()

    # Provision the tenant.
    session.add(
        PartnerTenant(
            id=uuid.uuid4(),
            microsoft_tenant_id=tid,
            partner_name="Test Partner",
        )
    )
    await session.commit()

    payload = {
        "iss": f"https://login.microsoftonline.com/{tid}/v2.0",
        "tid": tid,
        "sub": "subject-123",
        "oid": "oid-123",
        "preferred_username": "alice@partner.test",
        "aud": settings.entra_direct_audience,
        "iat": int(time.time()),
        "exp": int(time.time()) + 60,
    }
    token = jwt.encode(payload, priv, algorithm="RS256")

    class _StubKey:
        def __init__(self, pem: str) -> None:
            self.key = pem

    class _StubJwk:
        def get_signing_key_from_jwt(self, _token: str) -> _StubKey:
            return _StubKey(pub)

    # The lru_cache on get_entra_jwks_client means we can't monkeypatch the
    # function itself; replace the import sites used by the decoder.
    import jp_adopt_api.auth_entra as ae

    async def _stub_get_entra_jwks_client(_tid: str) -> _StubJwk:
        return _StubJwk()

    monkeypatch.setattr(ae, "get_entra_jwks_client", _stub_get_entra_jwks_client)

    try:
        user = await decode_entra_direct_token(session, token, settings)
        assert user.tid == tid
        assert user.sub == "oid-123"
        assert user.email == "alice@partner.test"
    finally:
        await session.execute(
            delete(PartnerTenant).where(PartnerTenant.microsoft_tenant_id == tid)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_expired_token_raises_jwt_error(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(entra_direct_audience="api://jp-adopt-core")
    tid = str(uuid.uuid4())
    priv, pub = _gen_rsa_keypair()

    session.add(
        PartnerTenant(
            id=uuid.uuid4(),
            microsoft_tenant_id=tid,
            partner_name="Test Partner",
        )
    )
    await session.commit()

    past = int(time.time()) - 600
    payload = {
        "iss": f"https://login.microsoftonline.com/{tid}/v2.0",
        "tid": tid,
        "sub": "subject-123",
        "oid": "oid-123",
        "aud": settings.entra_direct_audience,
        "iat": past - 60,
        "exp": past,
    }
    token = jwt.encode(payload, priv, algorithm="RS256")

    class _StubKey:
        def __init__(self, pem: str) -> None:
            self.key = pem

    class _StubJwk:
        def get_signing_key_from_jwt(self, _token: str) -> _StubKey:
            return _StubKey(pub)

    import jp_adopt_api.auth_entra as ae

    async def _stub_get_entra_jwks_client(_tid: str) -> _StubJwk:
        return _StubJwk()

    monkeypatch.setattr(ae, "get_entra_jwks_client", _stub_get_entra_jwks_client)

    try:
        with pytest.raises(jwt.ExpiredSignatureError):
            await decode_entra_direct_token(session, token, settings)
    finally:
        await session.execute(
            delete(PartnerTenant).where(PartnerTenant.microsoft_tenant_id == tid)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_wrong_alg_raises_invalid_algorithm(
    session: AsyncSession,
) -> None:
    settings = Settings(entra_direct_audience="api://jp-adopt-core")
    tid = str(uuid.uuid4())
    # HS256 token (wrong alg) — must be rejected before JWKS lookup.
    payload = {
        "iss": f"https://login.microsoftonline.com/{tid}/v2.0",
        "tid": tid,
        "sub": "subject-123",
        "exp": int(time.time()) + 60,
    }
    token = jwt.encode(
        payload, "secret-key-that-is-32-bytes-long!!!", algorithm="HS256"
    )
    with pytest.raises(jwt.InvalidAlgorithmError):
        await decode_entra_direct_token(session, token, settings)
