from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://jp_adopt:jp_adopt@127.0.0.1:5432/jp_adopt"

    azure_ad_b2c_tenant_name: str = ""
    azure_ad_b2c_tenant_id: str = ""
    azure_ad_b2c_client_id: str = ""
    azure_ad_b2c_policy: str = "B2C_1_signupsignin1"
    azure_ad_b2c_audience: str = ""
    azure_ad_b2c_jwks_uri: str | None = None
    azure_ad_b2c_issuer: str | None = None

    strict_auth: bool = False

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
