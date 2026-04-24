from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache

import jwt
from jwt import PyJWKClient

from jp_adopt_api.config import Settings

logger = logging.getLogger(__name__)

DEV_BEARER_TOKEN = "dev-local"


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


def authenticate_bearer(token: str, settings: Settings) -> AuthUser:
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
    try:
        return decode_b2c_access_token(token, settings)
    except jwt.PyJWTError as e:
        logger.debug("JWT validation failed: %s", e)
        raise
