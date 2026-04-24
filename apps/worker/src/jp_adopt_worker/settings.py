from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://jp_adopt:jp_adopt@127.0.0.1:5434/jp_adopt"
    redis_url: str = "redis://127.0.0.1:6379/0"

    integration_webhook_url: str = ""
    webhook_hmac_secret: str = ""

    outbox_batch_size: int = 10
    post_timeout_seconds: float = 30.0
