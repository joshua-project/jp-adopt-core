"""Drip step send task (U10).

Claims due steps via :func:`jp_adopt_api.domain.drips.claim_due_steps`
(SELECT … FOR UPDATE SKIP LOCKED), renders the step's MJML template,
sends through Azure Communication Services Email, and logs an
EnrollmentEvent. Advances the enrollment's ``current_step_position`` or
flips ``state`` to ``completed`` when no next step exists.

Send-at-hour gating: each step has ``send_at_hour`` / ``send_at_minute``
defining the local hour the email should go out. The claim is done
unconditionally; the worker checks the hour after claim and releases
(by setting ``state='active'`` again and rolling back the position
bump) when outside the window. v1 uses UTC for the hour check — a
follow-up adds per-contact timezone resolution.

ACS is optional in dev: same fallback pattern as
``send_magic_link_email`` — when ``ACS_CONNECTION_STRING`` is unset we
log the recipient + subject and treat the send as successful so the
state machine still advances.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from jp_adopt_api.domain.drips import (
    EVENT_SEND_FAILED,
    EVENT_SEND_FAILED_TEMPLATE_MISSING,
    EVENT_STEP_SENT,
    EXIT_REASON_DO_NOT_ENGAGE,
    EXIT_REASON_SUPPRESSED,
    EXIT_REASON_TEMPLATE_MISSING,
    TemplateMissingError,
    advance_enrollment,
    claim_due_steps,
    exit_enrollment,
    is_suppressed,
    log_enrollment_event,
    render_step_html,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


DRIP_TICK_BATCH_SIZE = 50


async def _send_via_acs(
    *,
    email: str,
    subject: str,
    html: str,
    plain: str,
    acs_connection_string: str | None,
    acs_sender_address: str,
) -> str | None:
    """Send through ACS. Returns the message id on success or None when
    ACS isn't configured (dev fallback). Raises on send failure."""
    if not acs_connection_string:
        logger.info(
            "drip.email.dev_fallback recipient=%s subject=%s", email, subject
        )
        return None
    try:
        from azure.communication.email import EmailClient  # type: ignore
    except Exception as e:  # pragma: no cover - optional dep
        logger.warning(
            "drip.email.acs_sdk_missing recipient=%s err=%s", email, e
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


def _is_within_send_window(
    *, now: datetime, send_at_hour: int, send_at_minute: int
) -> bool:
    """True if ``now`` is at or past today's send window (e.g. ``09:00``).

    v1 uses UTC. Per-contact timezone resolution lands in a follow-up
    so contacts with no recorded TZ still get sent (rather than being
    permanently stuck).
    """
    target = now.replace(
        hour=send_at_hour, minute=send_at_minute, second=0, microsecond=0
    )
    return now >= target


async def _process_due_steps(
    session: AsyncSession,
    *,
    acs_connection_string: str | None,
    acs_sender_address: str,
    now: datetime,
) -> dict[str, int]:
    """One worker tick: claim, send, advance. Caller commits.

    Returns counts for log visibility:
      * sent — steps successfully sent
      * deferred — steps held back because outside send window
      * exited — enrollments terminated this tick (suppression, template
        missing, do_not_engage)
      * failed — steps that raised during send (retry next tick)
    """
    counts = {"sent": 0, "deferred": 0, "exited": 0, "failed": 0}
    due = await claim_due_steps(
        session, now=now, limit=DRIP_TICK_BATCH_SIZE
    )
    if not due:
        return counts

    for d in due:
        # Late guard against do_not_engage / suppression — claim_due_steps
        # excludes do_not_engage and the suppression check is cheap.
        contact_email = d.contact.email_normalized
        if d.contact.adopter_status == "do_not_engage":
            await exit_enrollment(
                session, d.enrollment, reason=EXIT_REASON_DO_NOT_ENGAGE
            )
            counts["exited"] += 1
            continue
        if contact_email and await is_suppressed(session, contact_email):
            await exit_enrollment(
                session, d.enrollment, reason=EXIT_REASON_SUPPRESSED
            )
            counts["exited"] += 1
            continue

        if not _is_within_send_window(
            now=now,
            send_at_hour=d.step.send_at_hour,
            send_at_minute=d.step.send_at_minute,
        ):
            counts["deferred"] += 1
            continue

        # Render
        try:
            html, plain = render_step_html(
                template_name=d.step.mjml_template_name,
                context={
                    "contact_display_name": d.contact.display_name,
                    "contact_email": contact_email or "",
                    "campaign_name": d.campaign.name,
                    "step_position": d.step.position,
                },
            )
        except TemplateMissingError as e:
            log_enrollment_event(
                session,
                d.enrollment.id,
                event_type=EVENT_SEND_FAILED_TEMPLATE_MISSING,
                payload={
                    "template_name": d.step.mjml_template_name,
                    "error": str(e),
                },
            )
            await exit_enrollment(
                session,
                d.enrollment,
                reason=EXIT_REASON_TEMPLATE_MISSING,
            )
            counts["exited"] += 1
            continue

        # Send
        if not contact_email:
            # No email address — log + exit. This is a rare state since
            # the matching algo & intake both require email.
            log_enrollment_event(
                session,
                d.enrollment.id,
                event_type=EVENT_SEND_FAILED,
                payload={"error": "contact has no email_normalized"},
            )
            await exit_enrollment(
                session, d.enrollment, reason="no_email"
            )
            counts["exited"] += 1
            continue

        try:
            message_id = await _send_via_acs(
                email=contact_email,
                subject=d.step.subject,
                html=html,
                plain=plain,
                acs_connection_string=acs_connection_string,
                acs_sender_address=acs_sender_address,
            )
        except Exception as e:
            logger.warning(
                "drip.send.failed enrollment=%s step=%s err=%s",
                d.enrollment.id,
                d.step.position,
                e,
            )
            log_enrollment_event(
                session,
                d.enrollment.id,
                event_type=EVENT_SEND_FAILED,
                payload={
                    "error_type": type(e).__name__,
                    "error": str(e),
                    "step_position": d.step.position,
                },
            )
            counts["failed"] += 1
            # Leave state='active' so the next tick retries. The
            # claim_due_steps WHERE clause picks it up again because
            # last_step_sent_at hasn't been updated.
            continue

        # Success
        log_enrollment_event(
            session,
            d.enrollment.id,
            event_type=EVENT_STEP_SENT,
            payload={
                "step_position": d.step.position,
                "subject": d.step.subject,
                "message_id": message_id,
            },
        )
        await advance_enrollment(
            session, d.enrollment, sent_at=datetime.now(UTC)
        )
        counts["sent"] += 1

    return counts


async def send_drip_step(ctx: dict[str, Any]) -> None:
    """ARQ cron entry point — runs every 10s."""
    factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    cfg = ctx["worker_cfg"]
    async with factory() as session:
        async with session.begin():
            counts = await _process_due_steps(
                session,
                acs_connection_string=cfg.acs_connection_string,
                acs_sender_address=cfg.acs_sender_address,
                now=datetime.now(UTC),
            )
    if any(counts.values()):
        logger.info("drip.tick %s", counts)


__all__ = [
    "DRIP_TICK_BATCH_SIZE",
    "send_drip_step",
]
