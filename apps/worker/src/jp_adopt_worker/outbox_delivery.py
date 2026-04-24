from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jp_adopt_api.models import Outbox

from jp_adopt_worker.settings import WorkerSettings

logger = logging.getLogger(__name__)

# Reclaim rows stuck in "claimed" state (worker crash mid-delivery).
CLAIM_STALE_AFTER = timedelta(minutes=5)


def canonical_json_body(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_body(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


async def _post_row(
    client: httpx.AsyncClient,
    webhook_url: str,
    secret: str,
    timeout: float,
    payload: dict,
    row_id: str,
) -> None:
    body = canonical_json_body(payload)
    signature = sign_body(secret, body)
    headers = {
        "Content-Type": "application/json",
        "X-JP-Signature": signature,
        "Idempotency-Key": row_id,
    }
    resp = await client.post(webhook_url, content=body, headers=headers, timeout=timeout)
    resp.raise_for_status()


async def process_outbox_batch(
    session_factory: async_sessionmaker[AsyncSession], cfg: WorkerSettings
) -> int:
    if not cfg.integration_webhook_url or not cfg.webhook_hmac_secret:
        return 0

    stale_before = datetime.now(timezone.utc) - CLAIM_STALE_AFTER
    row_uuid: uuid.UUID | None = None
    payload: dict | None = None
    event_type: str | None = None

    # Phase 1: short transaction — claim row only (no network I/O).
    async with session_factory() as session:
        async with session.begin():
            stmt = (
                select(Outbox)
                .where(
                    Outbox.processed_at.is_(None),
                    or_(Outbox.claimed_at.is_(None), Outbox.claimed_at < stale_before),
                )
                .order_by(Outbox.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return 0
            row.claimed_at = datetime.now(timezone.utc)
            row_uuid = row.id
            payload = dict(row.payload_json)
            event_type = row.event_type

    row_id = str(row_uuid)
    assert payload is not None

    # Phase 2: POST outside any DB transaction (no row locks during network I/O).
    try:
        async with httpx.AsyncClient() as client:
            await _post_row(
                client,
                cfg.integration_webhook_url,
                cfg.webhook_hmac_secret,
                cfg.post_timeout_seconds,
                payload,
                row_id,
            )
    except Exception:
        # Allow another worker / tick to retry after CLAIM_STALE_AFTER.
        async with session_factory() as session:
            async with session.begin():
                r = await session.get(Outbox, row_uuid)
                if r is not None and r.processed_at is None:
                    r.claimed_at = None
        raise

    # Phase 3: mark processed in a short transaction.
    async with session_factory() as session:
        async with session.begin():
            r = await session.get(Outbox, row_uuid)
            if r is not None:
                r.processed_at = datetime.now(timezone.utc)
                r.claimed_at = None

    logger.info("Delivered outbox id=%s event=%s", row_id, event_type)
    return 1
