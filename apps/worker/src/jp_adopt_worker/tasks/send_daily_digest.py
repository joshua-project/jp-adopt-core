"""Daily digest send task (U11).

ARQ cron entrypoint. Runs at the worker's natural cadence; gates
internally on ``now_eastern.hour == 9`` so the actual send only fires
once per day around 9am US/Eastern. The internal gate is safer than
relying on ARQ's tz-naive cron handling — DST transitions still
produce one digest per day.

Idempotency: each tick computes today's window
``[yesterday 09:00 ET, today 09:00 ET)``. If a ``digest_run`` with the
same ``window_start`` already exists in ``status='sent'`` or
``status='empty'``, the new tick exits early.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, time, timedelta, timezone
from typing import Any

from jp_adopt_api.domain.digest import (
    build_digest_for_window,
    render_digest_html,
)
from jp_adopt_api.models import DigestRecipient, DigestRun
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


# US/Eastern offset from UTC. We keep it simple — Postgres + ARQ in
# America/New_York is not standardized across environments. In v1 the
# worker container should run with ``TZ=America/New_York`` AND we
# additionally gate on a UTC-derived "is it 9 am ET" check that
# tolerates either configuration.
US_EASTERN_OFFSET_FROM_UTC_HOURS_STANDARD = -5  # EST
US_EASTERN_OFFSET_FROM_UTC_HOURS_DAYLIGHT = -4  # EDT


def _current_eastern_offset_hours(now_utc: datetime) -> int:
    """Best-effort EST/EDT detection without a tz database dep. DST in
    the US starts the second Sunday in March and ends the first Sunday
    in November.
    """
    # March 8 - November 7 (approximation; close enough for the 9am check).
    if now_utc.month < 3 or now_utc.month > 11:
        return US_EASTERN_OFFSET_FROM_UTC_HOURS_STANDARD
    if now_utc.month == 3:
        return (
            US_EASTERN_OFFSET_FROM_UTC_HOURS_DAYLIGHT
            if now_utc.day >= 14
            else US_EASTERN_OFFSET_FROM_UTC_HOURS_STANDARD
        )
    if now_utc.month == 11:
        return (
            US_EASTERN_OFFSET_FROM_UTC_HOURS_STANDARD
            if now_utc.day >= 7
            else US_EASTERN_OFFSET_FROM_UTC_HOURS_DAYLIGHT
        )
    return US_EASTERN_OFFSET_FROM_UTC_HOURS_DAYLIGHT


def _eastern_now(now_utc: datetime) -> datetime:
    offset_hours = _current_eastern_offset_hours(now_utc)
    eastern = timezone(timedelta(hours=offset_hours))
    return now_utc.astimezone(eastern)


def _todays_window_eastern(now_utc: datetime) -> tuple[datetime, datetime]:
    """Compute the 24-hour window ending at today's 9am ET. Returns
    UTC-aware datetimes so the caller can compare directly with
    `Match.recommended_at`."""
    eastern_now = _eastern_now(now_utc)
    eastern_today_9am = datetime.combine(
        eastern_now.date(), time(hour=9, minute=0), tzinfo=eastern_now.tzinfo
    )
    eastern_window_start = eastern_today_9am - timedelta(days=1)
    return (
        eastern_window_start.astimezone(UTC),
        eastern_today_9am.astimezone(UTC),
    )


async def _send_via_acs(
    *,
    email: str,
    subject: str,
    html: str,
    plain: str,
    acs_connection_string: str | None,
    acs_sender_address: str,
) -> str | None:
    """Send through ACS; dev fallback logs only and returns None."""
    if not acs_connection_string:
        logger.info(
            "digest.email.dev_fallback recipient=%s subject=%s",
            email,
            subject,
        )
        return None
    try:
        from azure.communication.email import EmailClient  # type: ignore
    except Exception as e:  # pragma: no cover - optional dep
        logger.warning(
            "digest.email.acs_sdk_missing recipient=%s err=%s", email, e
        )
        return None
    client = EmailClient.from_connection_string(acs_connection_string)
    message = {
        "senderAddress": acs_sender_address,
        "recipients": {"to": [{"address": email}]},
        "content": {
            "subject": subject,
            "plainText": plain,
            "html": html,
        },
    }
    poller = client.begin_send(message)
    result = await asyncio.wait_for(
        asyncio.to_thread(poller.result), timeout=30.0
    )
    return str(result)


async def _digest_already_sent(
    session: AsyncSession, window_start: datetime
) -> bool:
    existing = (
        await session.execute(
            select(DigestRun.status).where(
                DigestRun.window_start == window_start,
                DigestRun.status.in_(("sent", "empty")),
            )
        )
    ).scalar_one_or_none()
    return existing is not None


async def run_digest(
    session: AsyncSession,
    *,
    window_start: datetime,
    window_end: datetime,
    acs_connection_string: str | None,
    acs_sender_address: str,
) -> dict[str, int]:
    """Build + dispatch the digest for ``[window_start, window_end)``.
    Idempotent: if a ``sent`` or ``empty`` ``digest_run`` already exists
    for the window, returns immediately.

    Returns counts: ``recipients``, ``sent``, ``failed``, ``skipped``.
    """
    counts = {"recipients": 0, "sent": 0, "failed": 0, "skipped": 0}
    if await _digest_already_sent(session, window_start):
        logger.info(
            "digest.already_sent window_start=%s — skipping",
            window_start.isoformat(),
        )
        counts["skipped"] = -1  # sentinel for "no run performed"
        return counts

    plans = await build_digest_for_window(
        session, window_start=window_start, window_end=window_end
    )
    counts["recipients"] = len(plans)
    run_row = DigestRun(
        id=uuid.uuid4(),
        window_start=window_start,
        window_end=window_end,
        started_at=datetime.now(UTC),
        status="pending",
        recipient_count=len(plans),
        match_count=sum(len(p.matches) for p in plans),
    )
    session.add(run_row)
    await session.flush()

    if not plans:
        run_row.status = "empty"
        run_row.ended_at = datetime.now(UTC)
        return counts

    for plan in plans:
        try:
            html, plain = render_digest_html(plan=plan)
            subject = (
                f"JP Adoption — {plan.matches[0].recommended_at.date()} digest"
                if plan.matches
                else "JP Adoption — daily digest"
            )
            await _send_via_acs(
                email=plan.recipient_address,
                subject=subject,
                html=html,
                plain=plain,
                acs_connection_string=acs_connection_string,
                acs_sender_address=acs_sender_address,
            )
            session.add(
                DigestRecipient(
                    id=uuid.uuid4(),
                    digest_run_id=run_row.id,
                    recipient_address=plan.recipient_address,
                    recipient_kind=plan.recipient_kind,
                    facilitator_org_id=plan.facilitator_org_id,
                    match_count=len(plan.matches),
                    match_ids=[str(m.match_id) for m in plan.matches],
                    status="sent",
                    sent_at=datetime.now(UTC),
                )
            )
            counts["sent"] += 1
        except Exception as e:
            logger.warning(
                "digest.send.failed recipient=%s err=%s",
                plan.recipient_address,
                e,
            )
            session.add(
                DigestRecipient(
                    id=uuid.uuid4(),
                    digest_run_id=run_row.id,
                    recipient_address=plan.recipient_address,
                    recipient_kind=plan.recipient_kind,
                    facilitator_org_id=plan.facilitator_org_id,
                    match_count=len(plan.matches),
                    match_ids=[str(m.match_id) for m in plan.matches],
                    status="failed",
                    error=f"{type(e).__name__}: {e}",
                )
            )
            counts["failed"] += 1

    run_row.ended_at = datetime.now(UTC)
    run_row.status = "sent" if counts["failed"] == 0 else "failed"
    return counts


async def send_daily_digest(ctx: dict[str, Any]) -> None:
    """ARQ cron entry. Runs every 10 minutes (registration); internal
    gate fires the actual send only when the local Eastern time is in
    [09:00, 09:30) so DST transitions still produce exactly one digest
    per day."""
    now_utc = datetime.now(UTC)
    eastern_now = _eastern_now(now_utc)
    # Send window: 09:00-09:30 ET. The cron tick rate determines how
    # quickly we react inside that window; cap at one execution per
    # day via the digest_run idempotency check.
    if not (eastern_now.hour == 9 and eastern_now.minute < 30):
        return
    window_start, window_end = _todays_window_eastern(now_utc)
    factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    cfg = ctx["worker_cfg"]
    async with factory() as session:
        async with session.begin():
            counts = await run_digest(
                session,
                window_start=window_start,
                window_end=window_end,
                acs_connection_string=cfg.acs_connection_string,
                acs_sender_address=cfg.acs_sender_address,
            )
    if counts.get("skipped") != -1:
        logger.info(
            "digest.tick window=%s..%s counts=%s",
            window_start.isoformat(),
            window_end.isoformat(),
            counts,
        )


__all__ = [
    "_eastern_now",
    "_todays_window_eastern",
    "run_digest",
    "send_daily_digest",
]
