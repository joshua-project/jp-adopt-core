from __future__ import annotations

import pytest

from jp_adopt_api.auth import (
    DEV_BEARER_TOKEN,
    DevelopmentAuthForbiddenError,
    authenticate_bearer,
)
from jp_adopt_api.config import Settings


def test_production_env_rejects_strict_auth_off() -> None:
    with pytest.raises(ValueError, match="STRICT_AUTH must be true"):
        Settings(app_env="production", strict_auth=False)


def test_production_env_accepts_strict_auth_on() -> None:
    # N3/N5: production now also requires a non-default magic-link signing key
    # and an ACS connection string. Provide both so we still exercise the
    # strict-auth happy path here.
    s = Settings(
        app_env="production",
        strict_auth=True,
        magic_link_signing_key="a" * 48,
        acs_connection_string="endpoint=https://example.com;accesskey=x",
    )
    assert s.is_production


def test_prod_alias_env_var_name() -> None:
    s = Settings(
        app_env="prod",
        strict_auth=True,
        magic_link_signing_key="a" * 48,
        acs_connection_string="endpoint=https://example.com;accesskey=x",
    )
    assert s.is_production


def test_dev_local_forbidden_in_production_even_if_strict_auth_false() -> None:
    s = Settings.model_construct(app_env="production", strict_auth=False)
    with pytest.raises(DevelopmentAuthForbiddenError):
        authenticate_bearer(DEV_BEARER_TOKEN, s)


def test_dev_local_allowed_when_not_production_and_strict_auth_false() -> None:
    s = Settings(app_env="development", strict_auth=False)
    user = authenticate_bearer(DEV_BEARER_TOKEN, s)
    assert user.sub == "dev-local"


def test_production_rejects_default_magic_link_signing_key() -> None:
    """N3: the checked-in dev default key happens to be 44 bytes, which clears
    the >=32 byte entropy floor. Without an explicit literal check production
    could boot with a publicly known HS256 secret. Verify the dedicated guard
    rejects the literal."""
    with pytest.raises(ValueError, match="dev default"):
        Settings(
            app_env="production",
            strict_auth=True,
            magic_link_signing_key="dev-magic-link-signing-key-please-change-32b",
            acs_connection_string="endpoint=https://example.com;accesskey=x",
        )


def test_production_accepts_rotated_magic_link_signing_key() -> None:
    """Companion to the above: a non-default >=32 byte key boots fine."""
    s = Settings(
        app_env="production",
        strict_auth=True,
        magic_link_signing_key="a" * 48,
        acs_connection_string="endpoint=https://example.com;accesskey=x",
    )
    assert s.is_production
    assert s.magic_link_signing_key == "a" * 48


def test_production_requires_acs_connection_string() -> None:
    """N5: missing ACS connection string in production means the worker
    silently drops magic-link emails. Refuse to boot."""
    with pytest.raises(ValueError, match="ACS_CONNECTION_STRING"):
        Settings(
            app_env="production",
            strict_auth=True,
            magic_link_signing_key="a" * 48,
            acs_connection_string=None,
        )


def test_production_accepts_empty_acs_as_missing() -> None:
    """N5: empty string is just as bad as None — both result in no emails
    being sent. The ACS SDK treats empty string as unconfigured."""
    with pytest.raises(ValueError, match="ACS_CONNECTION_STRING"):
        Settings(
            app_env="production",
            strict_auth=True,
            magic_link_signing_key="a" * 48,
            acs_connection_string="",
        )
