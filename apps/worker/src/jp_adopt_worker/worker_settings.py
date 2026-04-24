"""ARQ worker entry configuration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jp_adopt_worker.outbox_delivery import process_outbox_batch
from jp_adopt_worker.settings import WorkerSettings as EnvSettings

logger = logging.getLogger(__name__)


async def drain_outbox(ctx: dict[str, Any]) -> None:
    factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    cfg: EnvSettings = ctx["worker_cfg"]
    total = 0
    backoff = 1.0
    for _ in range(cfg.outbox_batch_size):
        try:
            delivered = await process_outbox_batch(factory, cfg)
            if delivered == 0:
                break
            total += delivered
            backoff = 1.0
        except Exception as e:
            logger.warning(
                "Delivery attempt failed (will retry on next tick): %s; sleeping %.1fs",
                e,
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)
    if total:
        logger.info("Processed %s outbox row(s) this tick", total)


async def startup(ctx: dict[str, Any]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    cfg = EnvSettings()
    engine = create_async_engine(cfg.database_url, pool_pre_ping=True)
    ctx["worker_cfg"] = cfg
    ctx["session_factory"] = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    ctx["engine"] = engine
    if not cfg.integration_webhook_url or not cfg.webhook_hmac_secret:
        logger.warning(
            "Outbox delivery disabled: set INTEGRATION_WEBHOOK_URL and WEBHOOK_HMAC_SECRET to enable "
            "(this message is logged once at worker startup, not every cron tick)"
        )
    logger.info("ARQ worker started (cron drain + Redis)")


async def shutdown(ctx: dict[str, Any]) -> None:
    engine = ctx.get("engine")
    if engine is not None:
        await engine.dispose()
    logger.info("ARQ worker stopped")


def _redis_dsn() -> str:
    return EnvSettings().redis_url


class ArqWorkerSettings:
    redis_settings = RedisSettings.from_dsn(_redis_dsn())
    on_startup = startup
    on_shutdown = shutdown
    cron_jobs = [
        cron(drain_outbox, second={0, 10, 20, 30, 40, 50}),
    ]
    functions = [drain_outbox]
