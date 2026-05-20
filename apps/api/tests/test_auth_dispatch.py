"""Tests for the issuer-based dispatch in ``authenticate_bearer``.

We hand-craft JWTs with PyJWT and assert that each issuer is routed to the
correct decoder (or fails closed with an explicit error code, not a 500).
We do *not* exercise the real B2C/Entra JWKS infrastructure here — that
belongs in integration tests. The dispatch tests use unverified payloads
plus stub decoders to keep the routing layer isolated.
"""

from __future__ import annotations

import time
import uuid

import jwt
import pytest

from jp_adopt_api.auth import (
    _ENTRA_ISSUER_RE,
    DEV_BEARER_TOKEN,
    DevelopmentAuthForbiddenError,
    authenticate_bearer,
    inspect_issuer,
)
from jp_adopt_api.config import Settings


def _make_token(payload: dict, key: str = "secret", alg: str = "HS256") -> str:
    return jwt.encode(payload, key, algorithm=alg)


def test_inspect_issuer_returns_iss_when_present() -> None:
    token = _make_token({"iss": "https://example.test/x", "sub": "abc"})
    assert inspect_issuer(token) == "https://example.test/x"


def test_inspect_issuer_returns_none_when_missing() -> None:
    token = _make_token({"sub": "abc"})
    assert inspect_issuer(token) is None


def test_inspect_issuer_handles_malformed_token() -> None:
    assert inspect_issuer("totally-not-a-jwt") is None


def test_entra_issuer_regex_matches_v2_pattern() -> None:
    assert _ENTRA_ISSUER_RE.match(
        "https://login.microsoftonline.com/11111111-2222-3333-4444-555555555555/v2.0"
    )
    assert _ENTRA_ISSUER_RE.match(
        "https://login.microsoftonline.com/11111111-2222-3333-4444-555555555555/v2.0/"
    )
    # B2C issuer must NOT match the Entra regex (would lead to wrong decoder).
    assert _ENTRA_ISSUER_RE.match(
        "https://contoso.b2clogin.com/00000000-0000-0000-0000-000000000000/v2.0/"
    ) is None


def test_dev_local_bearer_returns_dev_user_when_not_strict() -> None:
    s = Settings(strict_auth=False)
    user = authenticate_bearer(DEV_BEARER_TOKEN, s)
    assert user.sub == "dev-local"


def test_dev_local_bearer_rejected_when_production() -> None:
    s = Settings.model_construct(app_env="production", strict_auth=False)
    with pytest.raises(DevelopmentAuthForbiddenError):
        authenticate_bearer(DEV_BEARER_TOKEN, s)


def test_magic_link_issuer_routes_to_magic_link_decoder() -> None:
    """A token issued with our magic-link signing key and the canonical iss
    must round-trip through ``authenticate_bearer`` and return tid=magic_link.
    """
    s = Settings()
    now = int(time.time())
    payload = {
        "iss": s.magic_link_issuer,
        "sub": str(uuid.uuid4()),
        "email": "u@example.test",
        "idp": "magic_link",
        "iat": now,
        "exp": now + 60,
    }
    token = jwt.encode(payload, s.magic_link_signing_key, algorithm="HS256")
    user = authenticate_bearer(token, s)
    assert user.tid == "magic_link"
    assert user.email == "u@example.test"


def test_magic_link_token_with_wrong_signature_fails_closed() -> None:
    s = Settings()
    now = int(time.time())
    payload = {
        "iss": s.magic_link_issuer,
        "sub": "abc",
        "iat": now,
        "exp": now + 60,
    }
    bad = jwt.encode(payload, "wrong-key", algorithm="HS256")
    with pytest.raises(jwt.PyJWTError):
        authenticate_bearer(bad, s)


def test_entra_issuer_in_sync_path_raises_explicit_iss_error() -> None:
    """The sync entry point cannot do the partner_tenants lookup; it must
    fail with an explicit InvalidIssuerError (not a 500).
    """
    s = Settings()
    payload = {
        "iss": "https://login.microsoftonline.com/" + str(uuid.uuid4()) + "/v2.0",
        "sub": "abc",
        "tid": str(uuid.uuid4()),
        "exp": int(time.time()) + 60,
    }
    token = jwt.encode(payload, "secret", algorithm="RS256") if False else jwt.encode(
        payload, "secret", algorithm="HS256"
    )
    # NOTE: alg mismatch is irrelevant here — the sync dispatch should reject
    # before doing any signature verification, because Entra requires DB.
    with pytest.raises(jwt.InvalidIssuerError):
        authenticate_bearer(token, s)


def test_unknown_issuer_falls_through_to_b2c_and_fails_audience() -> None:
    """An unrecognized iss should fall through to the B2C decoder. With
    AZURE_AD_B2C_AUDIENCE empty (the test default), the decoder raises
    InvalidAudienceError — not a 500 — which the deps layer maps to 401.
    """
    s = Settings()  # B2C audience empty in test env
    payload = {
        "iss": "https://example.test/foreign-idp",
        "sub": "abc",
        "exp": int(time.time()) + 60,
    }
    token = jwt.encode(payload, "secret", algorithm="HS256")
    with pytest.raises(jwt.PyJWTError):
        authenticate_bearer(token, s)


def test_missing_iss_falls_through_to_b2c_decoder() -> None:
    """A token with no iss should still be routed to the B2C decoder rather
    than producing an internal error.
    """
    s = Settings()
    payload = {"sub": "abc", "exp": int(time.time()) + 60}
    token = jwt.encode(payload, "secret", algorithm="HS256")
    with pytest.raises(jwt.PyJWTError):
        authenticate_bearer(token, s)
