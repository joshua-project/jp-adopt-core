from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jp_adopt_api.models import Outbox

from jp_adopt_worker.settings import WorkerSettings

logger = logging.getLogger(__name__)


def canonical_json_body(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_body(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


async def _post_row(
    client: httpx.AsyncClient,
    row: Outbox,
    webhook_url: str,
    secret: str,
    timeout: float,
) -> None:
    body = canonical_json_body(dict(row.payload_json))
    signature = sign_body(secret, body)
    headers = {
        "Content-Type": "application/json",
        "X-JP-Signature": signature,
        "Idempotency-Key": str(row.id),
    }
    resp = await client.post(webhook_url, content=body, headers=headers, timeout=timeout)
    resp.raise_for_status()


async def process_outbox_batch(
    session_factory: async_sessionmaker[AsyncSession], cfg: WorkerSettings
) -> int:
    if not cfg.integration_webhook_url:
        logger.warning("INTEGRATION_WEBHOOK_URL is empty; skipping delivery")
        return 0
    if not cfg.webhook_hmac_secret:
        logger.warning("WEBHOOK_HMAC_SECRET is empty; skipping delivery")
        return 0

    delivered = 0
    backoff = 1.0
    async with httpx.AsyncClient() as client:
        for _ in range(cfg.outbox_batch_size):
            try:
                async with session_factory() as session:
                    async with session.begin():
                        stmt = (
                            select(Outbox)
                            .where(Outbox.processed_at.is_(None))
                            .order_by(Outbox.created_at)
                            .limit(1)
                            .with_for_update(skip_locked=True)
                        )
                        row = (await session.execute(stmt)).scalar_one_or_none()
                        if row is None:
                            break
                        await _post_row(
                            client,
                            row,
                            cfg.integration_webhook_url,
                            cfg.webhook_hmac_secret,
                            cfg.post_timeout_seconds,
                        )
                        row.processed_at = datetime.now(timezone.utc)
                        rid, etype = row.id, row.event_type
                delivered += 1
                backoff = 1.0
                logger.info("Delivered outbox id=%s event=%s", rid, etype)
            except Exception as e:
                logger.warning(
                    "Delivery attempt failed (will retry on next tick): %s; sleeping %.1fs",
                    e,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)

    return delivered
