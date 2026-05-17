from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache

import jwt
from jwt import PyJWKClient
from sqlalchemy.ext.asyncio import AsyncSession

from jp_adopt_api.config import Settings

logger = logging.getLogger(__name__)

DEV_BEARER_TOKEN = "dev-local"

# Issuer-prefix patterns for dispatch. We compile these once at import time so
# the hot path (every authenticated request) just runs a quick regex match.
_B2C_ISSUER_RE = re.compile(r"^https://[a-z0-9-]+\.b2clogin\.com/")
_ENTRA_ISSUER_RE = re.compile(
    r"^https://login\.microsoftonline\.com/[0-9a-fA-F-]{36}/v2\.0/?$"
)
# Magic-link issuer is configurable via Settings.magic_link_issuer; we compare
# directly against settings rather than baking the URL into a regex here.


class DevelopmentAuthForbiddenError(Exception):
    """dev-local bearer used while APP_ENV/ENV is production."""


@dataclass(frozen=True)
class AuthUser:
    sub: str
    email: str | None = None
    tid: str | None = None


@lru_cache
def _jwks_client(jwks_uri: str) -> PyJWKClient:
    return PyJWKClient(
        jwks_uri,
        cache_keys=True,
        max_cached_keys=16,
        lifespan=3600,
    )


def inspect_issuer(token: str) -> str | None:
    """Read the unverified ``iss`` claim. Returns None on malformed input.

    This is the dispatch primitive: we look at iss only to decide which
    decoder to invoke; every decoder still verifies signature + iss itself.
    """
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.DecodeError:
        return None
    iss = unverified.get("iss")
    return str(iss) if iss else None


def decode_b2c_access_token(token: str, settings: Settings) -> AuthUser:
    """Validate an Azure AD B2C access JWT (RS256, JWKS)."""
    if not settings.azure_ad_b2c_audience:
        raise jwt.InvalidAudienceError("AZURE_AD_B2C_AUDIENCE is not configured")
    jwks_uri = settings.b2c_jwks_uri
    issuer = settings.b2c_expected_issuer
    if not settings.azure_ad_b2c_tenant_name or not settings.azure_ad_b2c_tenant_id:
        raise jwt.InvalidIssuerError("B2C tenant is not configured")

    jwk_client = _jwks_client(jwks_uri)
    signing_key = jwk_client.get_signing_key_from_jwt(token)
    payload = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=settings.azure_ad_b2c_audience,
        issuer=issuer,
        options={"verify_aud": True, "verify_iss": True, "require": ["exp", "sub"]},
    )
    raw_emails = payload.get("emails")
    if isinstance(raw_emails, list) and raw_emails:
        email = raw_emails[0]
    else:
        email = payload.get("email")
    tid = payload.get("tid") or payload.get("tfp")
    return AuthUser(sub=str(payload["sub"]), email=email, tid=str(tid) if tid else None)


def _is_magic_link_issuer(iss: str, settings: Settings) -> bool:
    """Match either the configured issuer exactly, or a path-prefix family
    (``https://api.joshuaproject.net/magic-link/*``). The path-prefix form
    lets us version the issuer without changing config in lock-step.
    """
    configured = settings.magic_link_issuer
    if iss == configured:
        return True
    # Allow any version suffix under the same path family.
    base = configured.rsplit("/", 1)[0] + "/"
    return iss.startswith(base)


def authenticate_bearer(token: str, settings: Settings) -> AuthUser:
    """Sync dispatch: handles dev-local and B2C only.

    Multi-IdP tokens (Entra direct, magic-link) require either DB or
    JWT-only state and are dispatched here too, except Entra direct, which
    needs a DB session and is dispatched via :func:`authenticate_bearer_async`.
    """
    if token == DEV_BEARER_TOKEN:
        if settings.is_production:
            raise DevelopmentAuthForbiddenError(
                "Bearer dev-local is not allowed when APP_ENV/ENV is production"
            )
        if settings.strict_auth:
            raise jwt.InvalidTokenError(
                "Development bearer token is not allowed when STRICT_AUTH=true"
            )
        return AuthUser(sub="dev-local", email="dev@local.invalid", tid=None)

    iss = inspect_issuer(token)
    if iss is None:
        # No iss → fall through to B2C decoder (which will raise a precise error).
        return decode_b2c_access_token(token, settings)

    if _is_magic_link_issuer(iss, settings):
        # Lazy import to avoid an auth.py ↔ auth_magic.py circular import on
        # cold start (auth_magic imports AuthUser from auth).
        from jp_adopt_api.auth_magic import decode_magic_link_token

        return decode_magic_link_token(token, settings)

    if _ENTRA_ISSUER_RE.match(iss):
        # Entra direct requires DB lookup against partner_tenants. The sync
        # entry point cannot do that; raise a precise error so the caller
        # knows to use the async variant.
        raise jwt.InvalidIssuerError(
            "Entra direct tokens must be authenticated via "
            "authenticate_bearer_async (DB session required for partner_tenants)"
        )

    if _B2C_ISSUER_RE.match(iss):
        try:
            return decode_b2c_access_token(token, settings)
        except jwt.PyJWTError as e:
            logger.debug("JWT validation failed: %s", e)
            raise

    # Unknown issuer: in non-production we fall through to the B2C decoder
    # (which will fail with a precise error). In production we never accept
    # unknown issuers — STRICT_AUTH=true guarantees the dev-bearer above is
    # also gated, so the B2C decoder remains the canonical reject path.
    return decode_b2c_access_token(token, settings)


async def authenticate_bearer_async(
    session: AsyncSession,
    token: str,
    settings: Settings,
) -> AuthUser:
    """Async dispatch — same as :func:`authenticate_bearer` but also handles
    Entra direct (which needs a DB session for the ``partner_tenants`` check).
    """
    if token == DEV_BEARER_TOKEN:
        return authenticate_bearer(token, settings)

    iss = inspect_issuer(token)
    if iss is not None and _ENTRA_ISSUER_RE.match(iss):
        from jp_adopt_api.auth_entra import decode_entra_direct_token

        return await decode_entra_direct_token(session, token, settings)

    return authenticate_bearer(token, settings)
