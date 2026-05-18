"""Send a magic-link sign-in email via Azure Communication Services Email.

ACS is optional in dev: if ``ACS_CONNECTION_STRING`` is not set, we log the
click URL to stdout instead of sending. This is the documented dev fallback
in ``docs/runbooks/magic-link-side-car.md``.

ARQ entry point: ``send_magic_link_email``. The inline wrapper
``send_magic_link_email_inline`` is what the FastAPI BackgroundTasks scheduler
hands off when the API is run without ARQ (e.g. dev or tests).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

EMAIL_SUBJECT = "Sign in to Joshua Project Adoption"


def _build_email_body(click_url: str) -> tuple[str, str]:
    """Return ``(plain_text, html)`` versions of the email body."""
    plain = (
        "Click the link below to sign in to Joshua Project Adoption.\n\n"
        f"{click_url}\n\n"
        "This link expires in 15 minutes and can be used only once.\n"
        "If you did not request a sign-in link, you can safely ignore this email.\n"
    )
    html = f"""\
<!doctype html>
<html><body style="font-family: sans-serif; max-width: 560px; margin: 0 auto;">
  <h2 style="color: #1f2937;">Sign in to Joshua Project Adoption</h2>
  <p>Click the button below to finish signing in.</p>
  <p>
    <a href="{click_url}"
       style="display:inline-block;padding:10px 16px;background:#1f2937;
              color:#fff;text-decoration:none;border-radius:6px;">
      Sign in
    </a>
  </p>
  <p style="color:#6b7280;font-size:14px;">
    This link expires in 15 minutes and can be used only once.
    If you did not request a sign-in link, you can safely ignore this email.
  </p>
  <p style="color:#6b7280;font-size:12px;">
    If the button does not work, paste this URL into your browser:<br>
    <span style="word-break:break-all;">{click_url}</span>
  </p>
</body></html>
"""
    return plain, html


async def send_magic_link_email_inline(
    *,
    email: str,
    raw_token: str,
    click_url_base: str,
    acs_connection_string: str | None,
    acs_sender_address: str,
) -> None:
    """Send the magic-link email. In dev (no ACS connection string), logs
    the URL to stdout instead.
    """
    click_url = f"{click_url_base.rstrip('/')}/auth/claim?token={raw_token}"

    if not acs_connection_string:
        # Dev fallback: never log the click URL — it embeds the raw token, a
        # single-use bearer secret. Log only enough to confirm the path fired
        # and which recipient it would have been sent to.
        logger.info(
            "magic_link.email.dev_fallback recipient=%s",
            email,
        )
        return

    try:
        # Imported lazily so dev environments without ACS configured do not
        # need the SDK installed for the worker to import.
        from azure.communication.email import EmailClient  # type: ignore
    except Exception as e:  # pragma: no cover - depends on optional dep
        logger.warning(
            "magic_link.email.acs_sdk_missing recipient=%s err=%s",
            email,
            e,
        )
        return

    client = EmailClient.from_connection_string(acs_connection_string)
    plain, html = _build_email_body(click_url)
    message = {
        "senderAddress": acs_sender_address,
        "recipients": {"to": [{"address": email}]},
        "content": {
            "subject": EMAIL_SUBJECT,
            "plainText": plain,
            "html": html,
        },
    }
    poller = client.begin_send(message)
    # F10: poller.result() is synchronous and can block for the full HTTP
    # round-trip. Dispatch it to a worker thread with a hard 30s timeout so
    # a stalled ACS endpoint cannot freeze the asyncio loop. On timeout we
    # raise so ARQ's retry policy fires (capped at max_tries=2 per F41).
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(poller.result), timeout=30.0
        )
    except asyncio.TimeoutError:
        logger.error("magic_link.email.acs_timeout recipient=%s", email)
        raise
    logger.info("magic_link.email.sent recipient=%s message_id=%s", email, result)


# F41: cap ARQ retries at 2 so a partial failure on a transient ACS error
# doesn't result in N duplicate emails to the same recipient. ARQ reads
# ``max_tries`` from the task function object when scheduling.
send_magic_link_email_max_tries = 2


async def send_magic_link_email(ctx: dict[str, Any], **kwargs: Any) -> None:
    """ARQ wrapper: same signature contract, takes the worker ctx.

    B5: log a ``magic_link.email.permanent_failure`` event when ARQ is
    about to give up (i.e. this is the last retry). Without it, a flaky
    ACS endpoint silently swallows the user's sign-in request: ARQ retries
    twice, the second attempt fails, and the user simply never gets an
    email. The 202 envelope is anti-enumeration, so the user has no way
    to tell from the response either.

    ARQ provides ``job_try`` and ``max_tries`` in ``ctx``; log on the
    final attempt before re-raising.
    """
    try:
        await send_magic_link_email_inline(**kwargs)
    except Exception as e:
        job_try = ctx.get("job_try")
        max_tries = ctx.get("max_tries")
        if (
            isinstance(job_try, int)
            and isinstance(max_tries, int)
            and job_try >= max_tries
        ):
            logger.error(
                "magic_link.email.permanent_failure recipient=%s error_type=%s",
                kwargs.get("email"),
                type(e).__name__,
            )
        raise


# Surface the max_tries hint to ARQ via attribute lookup; ARQ inspects the
# function for ``max_tries`` (see arq.jobs). We expose it as both the
# module-level constant above (for documentation) and as an attribute on
# the function (where the scheduler actually reads it).
send_magic_link_email.max_tries = send_magic_link_email_max_tries  # type: ignore[attr-defined]
