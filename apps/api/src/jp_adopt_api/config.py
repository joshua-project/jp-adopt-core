from __future__ import annotations

from functools import lru_cache
from typing import Self

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Module-level literal for the dev default magic-link signing key. Exposed as
# a constant so the production guard validator can compare against it without
# duplicating the string (and so a test importing it stays in sync).
_DEV_MAGIC_LINK_SIGNING_KEY = "dev-magic-link-signing-key-please-change-32b"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = "postgresql+asyncpg://jp_adopt:jp_adopt@127.0.0.1:5434/jp_adopt"

    azure_ad_b2c_tenant_name: str = ""
    azure_ad_b2c_tenant_id: str = ""
    azure_ad_b2c_client_id: str = ""
    azure_ad_b2c_policy: str = "B2C_1_signupsignin1"
    azure_ad_b2c_audience: str = ""
    azure_ad_b2c_jwks_uri: str | None = None
    azure_ad_b2c_issuer: str | None = None

    app_env: str = Field(
        default="development",
        validation_alias=AliasChoices("APP_ENV", "ENV"),
    )

    strict_auth: bool = False

    # Comma-separated browser origins for CORS when APP_ENV is production (e.g. https://crm.example.com).
    cors_allow_origins: str = ""

    # NOTE: ``identity_provider`` field deleted in F17 (PR #29). The IdP set
    # is decided by the validator dispatch logic in
    # ``auth.authenticate_bearer_async`` (issuer regex matching against
    # B2C / Entra / magic-link), NOT by a config discriminator. Having both
    # would have invited the two to drift.

    # Magic-link side-car: HMAC signing key for the HS256 JWT minted after a
    # successful claim. Must be >= 32 bytes in production (model_validator below).
    # N3: the literal below is also rejected in production even though it
    # satisfies the >=32 byte floor — see
    # ``magic_link_key_must_not_be_default_in_production``.
    magic_link_signing_key: str = _DEV_MAGIC_LINK_SIGNING_KEY

    # The public web URL where the click target lives; the worker composes a link
    # of the form f"{click_base_url}/auth/claim?token={raw_token}".
    magic_link_click_base_url: str = "http://localhost:3000"

    # The issuer claim minted on magic-link JWTs. Verified on every claim.
    magic_link_issuer: str = "https://api.joshuaproject.net/magic-link/v1"

    # Entra direct side-car: aud claim the API requires on multi-tenant Entra tokens.
    # This is the Application ID URI of the API app registration in Entra.
    entra_direct_audience: str = "api://jp-adopt-core"

    # Azure Communication Services Email (worker uses this; API only reads
    # for the example .env). Optional in dev — when unset, the worker logs the
    # magic-link URL to stdout instead of sending.
    acs_connection_string: str | None = None
    acs_sender_address: str = "donotreply@joshuaproject.net"

    # Intake endpoints (U4): bearer API key auth for server-to-server calls
    # from jp-adopt-forms. Multi-key rotation is a v2 concern; single shared
    # secret is sufficient for week 1. Comma-separated to allow staged rotation
    # (forms can send the new key while the old is still accepted).
    intake_api_keys: str = ""
    # Default origin tag for submissions that don't set one explicitly. Form B
    # public-website submissions land here; Form A facilitator submissions
    # likewise. Override per-submission via the `origin` field on the body.
    intake_default_origin: str = "website"

    @property
    def intake_api_keys_list(self) -> list[str]:
        """Parsed list of acceptable intake bearer tokens.

        Empty list disables intake auth — only safe in dev. The intake router
        refuses to start in production with an empty list (see endpoint guard).
        """
        return [k.strip() for k in self.intake_api_keys.split(",") if k.strip()]

    @model_validator(mode="after")
    def production_requires_strict_auth(self) -> Self:
        if self.is_production and not self.strict_auth:
            msg = (
                "STRICT_AUTH must be true when APP_ENV or ENV is production (or prod). "
                "The development bearer token bypass is not allowed in production."
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def magic_link_key_must_be_strong_in_production(self) -> Self:
        if self.is_production and len(self.magic_link_signing_key.encode("utf-8")) < 32:
            msg = (
                "MAGIC_LINK_SIGNING_KEY must be at least 32 bytes long when "
                "APP_ENV/ENV is production (HS256 secret entropy floor)."
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def magic_link_key_must_not_be_default_in_production(self) -> Self:
        """N3: the dev default literal (44 bytes) satisfies the >=32-byte
        entropy floor above, which would otherwise let production boot with a
        publicly-known signing key. Reject the exact literal explicitly so an
        operator who forgets to rotate the key fails fast at startup instead
        of silently signing magic-link JWTs with a checked-in secret."""
        if (
            self.is_production
            and self.magic_link_signing_key == _DEV_MAGIC_LINK_SIGNING_KEY
        ):
            msg = (
                "MAGIC_LINK_SIGNING_KEY equals the dev default literal; rotate "
                "to a unique key in production (the dev default is checked into "
                "the repo and trivially recoverable)."
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def acs_connection_string_required_in_production(self) -> Self:
        """N5: without an ACS connection string the magic-link worker silently
        logs the magic-link URL to stdout instead of emailing it. In production
        that means a user submits a magic-link request, receives a 202 (anti-
        enumeration shape), and never gets the email — the misconfiguration
        is invisible from the outside because the success envelope is identical
        whether email delivery succeeded or not. Refuse to boot when ACS is
        unconfigured in production."""
        if self.is_production and not self.acs_connection_string:
            msg = (
                "ACS_CONNECTION_STRING must be set when APP_ENV/ENV is production. "
                "Without it the magic-link worker silently drops emails (logging "
                "the URL to stdout instead of sending) and users will get 202 "
                "responses with no email arriving."
            )
            raise ValueError(msg)
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() in ("production", "prod")

    @property
    def b2c_jwks_uri(self) -> str:
        if self.azure_ad_b2c_jwks_uri:
            return self.azure_ad_b2c_jwks_uri
        tn = self.azure_ad_b2c_tenant_name
        tid = self.azure_ad_b2c_tenant_id
        pol = self.azure_ad_b2c_policy
        return (
            f"https://{tn}.b2clogin.com/{tid}/{pol}/discovery/v2.0/keys"
        )

    @property
    def b2c_expected_issuer(self) -> str:
        if self.azure_ad_b2c_issuer:
            return self.azure_ad_b2c_issuer
        tn = self.azure_ad_b2c_tenant_name
        tid = self.azure_ad_b2c_tenant_id
        return f"https://{tn}.b2clogin.com/{tid}/v2.0/"


@lru_cache
def get_settings() -> Settings:
    return Settings()
