"""ARQ worker entry configuration."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from arq import cron
from arq.connections import RedisSettings
from jp_adopt_api.domain.drips import (
    enroll_on_event,
    exit_enrollments_for_contact,
)
from jp_adopt_api.models import Outbox
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jp_adopt_worker.outbox_delivery import process_outbox_batch
from jp_adopt_worker.settings import WorkerSettings as EnvSettings
from jp_adopt_worker.tasks.send_drip_step import send_drip_step
from jp_adopt_worker.tasks.send_magic_link_email import send_magic_link_email

# B4: batch size for purge DELETEs. Bounded so a single statement can't lock
# the table for seconds when accumulated rows are large.
_PURGE_BATCH_SIZE = 1000
# B4: small sleep between batches to give other transactions a chance to
# acquire row locks. Total throughput remains very high.
_PURGE_BATCH_SLEEP_S = 0.1
# B2: stuck-pending threshold for api_idempotency_keys. If the handler crashed
# after inserting the pending row but before completing it, the row would
# otherwise persist for the full 24h expiry window, blocking retries with the
# same key. One hour is well beyond any reasonable request lifetime.
_STUCK_PENDING_THRESHOLD_INTERVAL = "1 hour"

logger = logging.getLogger(__name__)


async def drain_outbox(ctx: dict[str, Any]) -> None:
    factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    cfg: EnvSettings = ctx["worker_cfg"]
    total = 0
    for _ in range(cfg.outbox_batch_size):
        try:
            delivered = await process_outbox_batch(factory, cfg)
            if delivered == 0:
                break
            total += delivered
        except Exception as e:
            # F38: fail fast on first delivery error inside this tick.
            # The previous backoff-sleep-and-retry loop chewed worker CPU
            # during outages and delayed unrelated rows; the 10s cron tick
            # is the natural retry cadence — bail out and pick up next tick.
            logger.warning(
                "Outbox delivery failed; deferring to next cron tick: %s", e
            )
            break
    if total:
        logger.info("Processed %s outbox row(s) this tick", total)


_DRIP_DRAIN_BATCH_SIZE = 50
_DO_NOT_ENGAGE_EVENT_TYPE = "jp.adopt.v1.contact.do_not_engage"


async def drain_drip_enrollments(ctx: dict[str, Any]) -> None:
    """U10: read recent Outbox events with a contact_id in the payload
    and enroll the contact into any active campaign whose
    ``trigger_event_type`` matches. Special-cases the
    ``contact.do_not_engage`` event to exit all open enrollments for
    that contact instead of enrolling.

    Marks each row's ``drip_processed_at`` so subsequent ticks skip it.
    The partial index ``ix_outbox_drip_unprocessed`` keeps the scan cheap
    no matter how large the Outbox grows.

    Idempotent on enrollment via the
    ``uq_enrollment_open_per_campaign_contact`` partial unique index —
    concurrent drains converge to one enrollment per contact + campaign.
    """
    factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    processed = 0
    try:
        async with factory() as session:
            async with session.begin():
                rows = (
                    await session.execute(
                        select(Outbox)
                        .where(Outbox.drip_processed_at.is_(None))
                        .order_by(Outbox.created_at.asc())
                        .limit(_DRIP_DRAIN_BATCH_SIZE)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars().all()
                for row in rows:
                    payload = row.payload_json or {}
                    contact_id_raw = payload.get("contact_id")
                    if contact_id_raw:
                        try:
                            contact_id = uuid.UUID(str(contact_id_raw))
                        except ValueError:
                            logger.warning(
                                "drip.drain.invalid_contact_id outbox=%s "
                                "raw=%r",
                                row.id,
                                contact_id_raw,
                            )
                            row.drip_processed_at = datetime.now(UTC)
                            continue
                        if row.event_type == _DO_NOT_ENGAGE_EVENT_TYPE:
                            exited = await exit_enrollments_for_contact(
                                session,
                                contact_id=contact_id,
                                reason="do_not_engage",
                            )
                            if exited:
                                logger.info(
                                    "drip.drain.do_not_engage contact=%s "
                                    "exited=%d",
                                    contact_id,
                                    exited,
                                )
                        else:
                            await enroll_on_event(
                                session,
                                event_type=row.event_type,
                                contact_id=contact_id,
                            )
                    row.drip_processed_at = datetime.now(UTC)
                    processed += 1
    except SQLAlchemyError as e:
        logger.warning("drip.drain.failed error=%s", e)
        return
    if processed:
        logger.info("drip.drain processed=%d", processed)


async def purge_magic_link_rate_limits(ctx: dict[str, Any]) -> None:
    """F33: drop magic-link rate-limit rows older than 2 hours.

    The rate-limit window is 1 hour; we keep an extra hour of headroom
    so a clock skew / cron tardiness never falsely lets a hot account in.

    B4: batch the DELETE and tolerate transient SQL errors. After weeks of
    accumulation an unbounded single DELETE could lock the table for
    seconds; the loop runs in 1000-row chunks with a 100ms sleep between
    batches. Failures log a warning rather than tipping the worker over.
    """
    factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    total = 0
    try:
        async with factory() as session:
            while True:
                result = await session.execute(
                    text(
                        "DELETE FROM magic_link_rate_limit "
                        "WHERE id IN ("
                        "  SELECT id FROM magic_link_rate_limit "
                        "  WHERE requested_at < now() - interval '2 hours' "
                        f"  LIMIT {_PURGE_BATCH_SIZE}"
                        ")"
                    )
                )
                await session.commit()
                rc = result.rowcount or 0
                total += rc
                if rc < _PURGE_BATCH_SIZE:
                    break
                await asyncio.sleep(_PURGE_BATCH_SLEEP_S)
    except SQLAlchemyError as e:
        logger.warning("purge.magic_link_rate_limit.failed error=%s", e)
        return
    if total:
        logger.info(
            "purge_magic_link_rate_limits: deleted %s row(s)", total
        )


async def purge_idempotency_keys(ctx: dict[str, Any]) -> None:
    """F34: drop idempotency-cache rows past their expires_at.

    Per-row TTL is set at INSERT time (server_default on ``expires_at``);
    this cron just sweeps anything already past that so the table doesn't
    grow unbounded as forms submit ever more keys.

    B2: also remove any row stuck in ``state='pending'`` for more than
    1 hour. If the handler crashed after inserting the pending row but
    before flipping to ``completed``, the row otherwise persists until
    ``expires_at`` (24h from insert) and blocks retries with the same
    idempotency key.

    B3 caveat: a client retrying at the 24h boundary races this purge. If
    the purge wins, the cached body is gone and the handler reprocesses,
    causing potential double side effects. Intake idempotency replay is
    documented as best-effort, not contract-guaranteed; this race is
    acceptable for v1. Future improvement: extend retention to 48h or
    add a ``claimed_for_replay`` heuristic.

    B4: batch DELETE and catch SQL errors (see purge_magic_link_rate_limits).
    """
    factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    total = 0
    try:
        async with factory() as session:
            while True:
                result = await session.execute(
                    text(
                        "DELETE FROM api_idempotency_keys "
                        "WHERE id IN ("
                        "  SELECT id FROM api_idempotency_keys "
                        "  WHERE expires_at < now() "
                        "     OR (state = 'pending' "
                        f"         AND created_at < now() - interval '{_STUCK_PENDING_THRESHOLD_INTERVAL}') "
                        f"  LIMIT {_PURGE_BATCH_SIZE}"
                        ")"
                    )
                )
                await session.commit()
                rc = result.rowcount or 0
                total += rc
                if rc < _PURGE_BATCH_SIZE:
                    break
                await asyncio.sleep(_PURGE_BATCH_SLEEP_S)
    except SQLAlchemyError as e:
        logger.warning("purge.api_idempotency_keys.failed error=%s", e)
        return
    if total:
        logger.info("purge_idempotency_keys: deleted %s row(s)", total)


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
        # U10: drip engine. Enrollment drain reads recent Outbox events
        # and creates Enrollment rows when a campaign's
        # trigger_event_type matches. Send drain claims due steps with
        # SKIP LOCKED and ships them via ACS. Both run every 10s offset
        # from the outbox webhook drain so concurrent locks are rare.
        cron(drain_drip_enrollments, second={5, 15, 25, 35, 45, 55}),
        cron(send_drip_step, second={5, 15, 25, 35, 45, 55}),
        # F33: hourly sweep of magic-link rate-limit rows older than 2h.
        # F34: hourly sweep of expired idempotency-cache rows.
        cron(purge_magic_link_rate_limits, minute=7),
        cron(purge_idempotency_keys, minute=23),
    ]
    functions = [drain_outbox, send_magic_link_email, send_drip_step]
