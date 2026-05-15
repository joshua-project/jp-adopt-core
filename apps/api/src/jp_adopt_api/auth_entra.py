"""Entra direct side-car: validate multi-tenant Azure AD access tokens issued
by *partner* tenants (not the consumer-IdP-fronted B2C tenant).

Why a side-car rather than a B2C user flow? Multi-tenant Entra is the model
that scales to N partner orgs without each partner needing to be federated
through our B2C. The cost is that we must enforce the ``partner_tenants``
allowlist ourselves — any tid that has not been provisioned by an operator
is rejected with 403 ``tenant_not_provisioned``.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jp_adopt_api.auth import AuthUser
from jp_adopt_api.config import Settings
from jp_adopt_api.models import PartnerTenant

logger = logging.getLogger(__name__)


class TenantNotProvisionedError(Exception):
    """The ``tid`` claim is not present in ``partner_tenants``."""


class EntraDiscoveryError(Exception):
    """Could not fetch or parse the v2 openid-configuration for the tid."""


def _discovery_url(tid: str) -> str:
    return f"https://login.microsoftonline.com/{tid}/v2.0/.well-known/openid-configuration"


def _expected_issuer(tid: str) -> str:
    return f"https://login.microsoftonline.com/{tid}/v2.0"


@lru_cache(maxsize=64)
def _cached_discovery(tid: str) -> dict[str, Any]:
    """Synchronously fetch the OIDC discovery document for ``tid``.

    Cached process-wide. PyJWKClient is itself synchronous, so we keep a sync
    httpx call here for symmetry; the surrounding decoder is async and
    delegates to a thread when needed.
    """
    resp = httpx.get(_discovery_url(tid), timeout=10.0)
    if resp.status_code != 200:
        raise EntraDiscoveryError(
            f"openid-configuration for tid={tid} returned {resp.status_code}"
        )
    body = resp.json()
    if "jwks_uri" not in body:
        raise EntraDiscoveryError(
            f"openid-configuration for tid={tid} missing jwks_uri"
        )
    return body


@lru_cache(maxsize=64)
def get_entra_jwks_client(tid: str) -> PyJWKClient:
    """Return a cached PyJWKClient for a partner tenant."""
    doc = _cached_discovery(tid)
    return PyJWKClient(
        doc["jwks_uri"],
        cache_keys=True,
        max_cached_keys=16,
        lifespan=3600,
    )


async def _tenant_is_provisioned(session: AsyncSession, tid: str) -> bool:
    stmt = select(PartnerTenant.id).where(PartnerTenant.microsoft_tenant_id == tid)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def decode_entra_direct_token(
    session: AsyncSession,
    token: str,
    settings: Settings,
) -> AuthUser:
    """Validate a multi-tenant Entra v2 access JWT.

    Steps:
      1. Parse header → confirm alg=RS256.
      2. Read unverified ``tid`` claim.
      3. Check ``partner_tenants`` allowlist.
      4. Fetch JWKS for that tid (cached); verify signature.
      5. Validate iss/aud/exp.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.DecodeError as e:
        raise jwt.InvalidTokenError(f"Malformed Entra token header: {e}") from None
    if header.get("alg") != "RS256":
        raise jwt.InvalidAlgorithmError(
            f"Entra direct tokens must be RS256; got {header.get('alg')!r}"
        )

    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.DecodeError as e:
        raise jwt.InvalidTokenError(f"Malformed Entra token body: {e}") from None
    tid = unverified.get("tid")
    if not tid:
        raise jwt.InvalidIssuerError("Entra token missing tid claim")

    if not await _tenant_is_provisioned(session, str(tid)):
        raise TenantNotProvisionedError(
            f"Microsoft tenant {tid} is not in partner_tenants"
        )

    jwk_client = get_entra_jwks_client(str(tid))
    signing_key = jwk_client.get_signing_key_from_jwt(token)
    payload = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=settings.entra_direct_audience,
        issuer=_expected_issuer(str(tid)),
        options={"verify_aud": True, "verify_iss": True, "require": ["exp", "sub"]},
    )
    sub = str(payload.get("oid") or payload["sub"])
    email = payload.get("preferred_username") or payload.get("email")
    return AuthUser(sub=sub, email=email, tid=str(tid))
