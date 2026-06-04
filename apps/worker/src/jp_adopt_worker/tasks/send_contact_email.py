"""Send a staff-composed email to a contact via Azure Communication Services.

F3 (#52 sibling). Mirrors ``send_magic_link_email``: ACS is optional in dev —
if ``ACS_CONNECTION_STRING`` is not set, we log a dev-fallback line instead of
sending. The one behavior beyond the magic-link path is the post-send update
of the originating ``activity_log`` (email note) ``source_metadata.status`` to
``sent`` / ``failed`` / ``logged`` so the record timeline reflects delivery.

The API schedules ``send_contact_email_inline`` via FastAPI BackgroundTasks
(see ``routers/contacts.py``); it runs in the API process post-response, so it
opens its own short-lived engine to update the note row.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jp_adopt_api.models import ActivityLog

logger = logging.getLogger(__name__)


def _build_email_body(subject: str, body: str) -> tuple[str, str]:
    """Return ``(plain_text, html)`` for a staff-composed message. The staff
    body is plain text; we wrap it in a minimal branded HTML shell and escape
    nothing beyond newline-to-<br> (the body is staff-authored, not attacker
    input, and ACS sends it to a known contact)."""
    plain = f"{body}\n"
    body_html = body.replace("\n", "<br>\n")
    html = f"""\
<!doctype html>
<html><body style="font-family: sans-serif; max-width: 560px; margin: 0 auto;">
  <h2 style="color: #1f2937;">{subject}</h2>
  <p style="color:#111827;font-size:15px;line-height:1.5;">{body_html}</p>
  <p style="color:#6b7280;font-size:12px;margin-top:24px;">
    Sent by Joshua Project Adoption staff.
  </p>
</body></html>
"""
    return plain, html


async def _update_note_status(
    database_url: str,
    note_id: uuid.UUID,
    status_value: str,
    *,
    message_id: str | None = None,
) -> None:
    """Stamp the email note's ``source_metadata.status`` (and message id on
    success). Reassigns the JSONB dict so SQLAlchemy emits the UPDATE."""
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as session:
            note = await session.get(ActivityLog, note_id)
            if note is None:
                logger.warning(
                    "contact_email.note_missing note_id=%s", note_id
                )
                return
            meta = dict(note.source_metadata or {})
            meta["status"] = status_value
            if message_id is not None:
                meta["message_id"] = str(message_id)
            note.source_metadata = meta
            await session.commit()
    finally:
        await engine.dispose()


async def send_contact_email_inline(
    *,
    note_id: uuid.UUID,
    recipients: list[str],
    subject: str,
    body: str,
    reply_to: str | None,
    acs_connection_string: str | None,
    acs_sender_address: str,
    database_url: str,
) -> None:
    """Send the email and stamp the note status. In dev (no ACS connection
    string), logs a dev-fallback line and marks the note ``logged``."""
    if not acs_connection_string:
        logger.info(
            "contact_email.dev_fallback note_id=%s recipients=%d",
            note_id,
            len(recipients),
        )
        await _update_note_status(database_url, note_id, "logged")
        return

    try:
        # Imported lazily so dev environments without ACS configured do not
        # need the SDK installed for the worker to import.
        from azure.communication.email import EmailClient  # type: ignore
    except Exception as e:  # pragma: no cover - depends on optional dep
        logger.warning(
            "contact_email.acs_sdk_missing note_id=%s err=%s", note_id, e
        )
        await _update_note_status(database_url, note_id, "failed")
        return

    plain, html = _build_email_body(subject, body)
    message: dict[str, Any] = {
        "senderAddress": acs_sender_address,
        "recipients": {"to": [{"address": r} for r in recipients]},
        "content": {"subject": subject, "plainText": plain, "html": html},
    }
    if reply_to:
        message["replyTo"] = [{"address": reply_to}]

    # F10 pattern: poller.result() blocks; dispatch to a thread with a hard
    # 30s timeout so a stalled ACS endpoint cannot freeze the loop. The whole
    # send (client build + begin_send + result) is guarded so any ACS error
    # marks the note ``failed`` rather than propagating out of the task.
    try:
        client = EmailClient.from_connection_string(acs_connection_string)
        poller = client.begin_send(message)
        result = await asyncio.wait_for(
            asyncio.to_thread(poller.result), timeout=30.0
        )
    except TimeoutError:
        logger.error("contact_email.acs_timeout note_id=%s", note_id)
        await _update_note_status(database_url, note_id, "failed")
        return
    except Exception as e:
        logger.error("contact_email.acs_error note_id=%s err=%s", note_id, e)
        await _update_note_status(database_url, note_id, "failed")
        return

    logger.info(
        "contact_email.sent note_id=%s message_id=%s", note_id, result
    )
    await _update_note_status(
        database_url, note_id, "sent", message_id=str(result)
    )
