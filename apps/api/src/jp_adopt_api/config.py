from __future__ import annotations

from functools import lru_cache
from typing import Literal, Self

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Declarative discriminator: only "b2c" is supported in week 1. Future weeks
    # may introduce alternate IdP strategies; this field exists so the choice is
    # encoded in config rather than inferred from runtime token issuers.
    identity_provider: Literal["b2c"] = "b2c"

    # Magic-link side-car: HMAC signing key for the HS256 JWT minted after a
    # successful claim. Must be >= 32 bytes in production (model_validator below).
    magic_link_signing_key: str = "dev-magic-link-signing-key-please-change-32b"

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
