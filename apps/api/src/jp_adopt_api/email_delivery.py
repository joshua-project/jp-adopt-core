"""Azure Communication Services email send.

Lives in ``jp_adopt_api`` (not the worker) so BOTH the API — e.g. the drip
test-send endpoint — and the worker can use it. The worker already depends on
``jp_adopt_api``; the API does not depend on the worker, so anything the API
needs to call at request time must live here. (Putting this only in the worker
package is why the test-send endpoint 503'd in production: the API runtime
container ships ``jp-adopt-api`` but not ``jp-adopt-worker``.)

The Azure SDK is imported lazily inside the function so merely importing this
module never requires ``azure-communication-email`` to be installed; a send
with ACS unconfigured (dev) returns ``None`` without touching the SDK.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def send_via_acs(
    *,
    email: str,
    subject: str,
    html: str,
    plain: str,
    acs_connection_string: str | None,
    acs_sender_address: str,
) -> str | None:
    """Send through ACS. Returns the message id on success, or ``None`` when ACS
    isn't configured (dev fallback — nothing delivered). Raises on send failure."""
    if not acs_connection_string:
        logger.info(
            "email.acs.dev_fallback recipient=%s subject=%s", email, subject
        )
        return None
    try:
        from azure.communication.email import EmailClient  # type: ignore
    except Exception as e:  # pragma: no cover - optional dep
        logger.warning("email.acs.sdk_missing recipient=%s err=%s", email, e)
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
    result = await asyncio.wait_for(asyncio.to_thread(poller.result), timeout=30.0)
    return str(result)
