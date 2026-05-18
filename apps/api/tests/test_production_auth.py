from __future__ import annotations

import pytest

from jp_adopt_api.auth import (
    DEV_BEARER_TOKEN,
    DevelopmentAuthForbiddenError,
    authenticate_bearer,
)
from jp_adopt_api.config import _DEV_MAGIC_LINK_SIGNING_KEY, Settings


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
    rejects the literal. Import the canonical constant from config so this test
    cannot drift from the runtime value."""
    with pytest.raises(ValueError, match="dev default"):
        Settings(
            app_env="production",
            strict_auth=True,
            magic_link_signing_key=_DEV_MAGIC_LINK_SIGNING_KEY,
            acs_connection_string="endpoint=https://example.com;accesskey=x",
        )


def test_production_rejects_default_magic_link_signing_key_with_whitespace() -> None:
    """N3 whitespace bypass: a trailing newline or padding space turns the
    literal-equality check into a no-op even though the runtime HS256 verifier
    sees the same effective secret. Normalize via strip() before comparing."""
    padded = f"  {_DEV_MAGIC_LINK_SIGNING_KEY}\n"
    with pytest.raises(ValueError, match="dev default"):
        Settings(
            app_env="production",
            strict_auth=True,
            magic_link_signing_key=padded,
            acs_connection_string="endpoint=https://example.com;accesskey=x",
        )


def test_production_rejects_default_magic_link_signing_key_with_case_variant() -> None:
    """N3 case bypass: an operator copy-pasting the literal from a wiki that
    uppercased it would otherwise pass the equality check yet still produce a
    publicly-known secret (HMAC keys are byte-sensitive but the *literal* is
    the threat model — both forms are documented in the repo)."""
    upper = _DEV_MAGIC_LINK_SIGNING_KEY.upper()
    with pytest.raises(ValueError, match="dev default"):
        Settings(
            app_env="production",
            strict_auth=True,
            magic_link_signing_key=upper,
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


def test_production_rejects_empty_acs_connection_string() -> None:
    """N5: empty string is just as bad as None — both result in no emails
    being sent. The ACS SDK treats empty string as unconfigured. (A6: renamed
    from ``test_production_accepts_empty_acs_as_missing`` — the prior name was
    inverted relative to the body.)"""
    with pytest.raises(ValueError, match="ACS_CONNECTION_STRING"):
        Settings(
            app_env="production",
            strict_auth=True,
            magic_link_signing_key="a" * 48,
            acs_connection_string="",
        )


def test_production_rejects_placeholder_acs_connection_string() -> None:
    """A4: 'TODO-fill-in-vault' satisfies ``not s.acs_connection_string`` but
    blows up at first send. The shape check rejects it at boot."""
    with pytest.raises(ValueError, match="does not look like a valid"):
        Settings(
            app_env="production",
            strict_auth=True,
            magic_link_signing_key="a" * 48,
            acs_connection_string="TODO-fill-in-vault",
        )


def test_production_rejects_noise_acs_connection_string() -> None:
    """A4: arbitrary non-empty noise without endpoint=/accesskey= substrings is
    likewise rejected at boot."""
    with pytest.raises(ValueError, match="does not look like a valid"):
        Settings(
            app_env="production",
            strict_auth=True,
            magic_link_signing_key="a" * 48,
            acs_connection_string="just-some-noise",
        )


def test_production_accepts_well_formed_acs_connection_string() -> None:
    """A4: a string that contains both ``endpoint=`` and ``accesskey=``
    substrings is accepted (case-insensitive)."""
    s = Settings(
        app_env="production",
        strict_auth=True,
        magic_link_signing_key="a" * 48,
        acs_connection_string="endpoint=https://x.communication.azure.com/;accesskey=abc",
    )
    assert s.is_production
