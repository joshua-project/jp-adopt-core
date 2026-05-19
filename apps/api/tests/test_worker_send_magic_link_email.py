"""Worker task: ``send_magic_link_email`` (ARQ wrapper) tests.

F-R-B5-1: ARQ 0.28.0 does NOT inject ``max_tries`` into ``ctx``. The wrapper
must compare ``ctx['job_try']`` against the module-level constant
``send_magic_link_email_max_tries`` to detect the final retry. The previous
implementation read ``ctx.get('max_tries')`` and required it to be an int,
so the isinstance branch never fired and the permanent_failure log was dead
code.

These tests exercise the wrapper with an injected failure and verify the
log fires (or doesn't) based on ``job_try``.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from jp_adopt_worker.tasks.send_magic_link_email import (
    send_magic_link_email,
    send_magic_link_email_max_tries,
)


@pytest.mark.asyncio
async def test_permanent_failure_logged_on_final_retry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When ``job_try`` reaches the max-tries cap, the wrapper must log a
    ``magic_link.email.permanent_failure`` event so an operator can spot the
    silent-drop case (user gets 202, never gets the email)."""

    async def _boom(**_kwargs):
        raise RuntimeError("acs unreachable")

    caplog.set_level(logging.ERROR)
    with patch(
        "jp_adopt_worker.tasks.send_magic_link_email.send_magic_link_email_inline",
        side_effect=_boom,
    ):
        with pytest.raises(RuntimeError, match="acs unreachable"):
            await send_magic_link_email(
                {"job_try": send_magic_link_email_max_tries},
                email="user@example.com",
                raw_token="t",
                click_url_base="http://x",
                acs_connection_string=None,
                acs_sender_address="from@example.com",
            )

    permanent_failure_records = [
        r for r in caplog.records if "magic_link.email.permanent_failure" in r.message
    ]
    assert len(permanent_failure_records) == 1
    msg = permanent_failure_records[0].message
    assert "user@example.com" in msg
    assert "RuntimeError" in msg


@pytest.mark.asyncio
async def test_permanent_failure_not_logged_on_non_final_retry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Companion: on retries below the cap the wrapper must NOT emit the
    permanent_failure log (ARQ will retry). Otherwise every failure shows
    up as 'permanent' and operators lose the signal."""

    async def _boom(**_kwargs):
        raise RuntimeError("acs unreachable")

    caplog.set_level(logging.ERROR)
    assert send_magic_link_email_max_tries >= 2  # sanity: test would be vacuous if 1
    with patch(
        "jp_adopt_worker.tasks.send_magic_link_email.send_magic_link_email_inline",
        side_effect=_boom,
    ):
        with pytest.raises(RuntimeError, match="acs unreachable"):
            await send_magic_link_email(
                {"job_try": send_magic_link_email_max_tries - 1},
                email="user@example.com",
                raw_token="t",
                click_url_base="http://x",
                acs_connection_string=None,
                acs_sender_address="from@example.com",
            )

    permanent_failure_records = [
        r for r in caplog.records if "magic_link.email.permanent_failure" in r.message
    ]
    assert permanent_failure_records == []


@pytest.mark.asyncio
async def test_permanent_failure_not_logged_when_job_try_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Defensive: if ARQ's ctx schema changes again and ``job_try`` goes
    missing, the wrapper should still re-raise (so retry logic outside the
    process can decide) but must not log permanent_failure on every attempt
    (which would mask the real signal)."""

    async def _boom(**_kwargs):
        raise RuntimeError("acs unreachable")

    caplog.set_level(logging.ERROR)
    with patch(
        "jp_adopt_worker.tasks.send_magic_link_email.send_magic_link_email_inline",
        side_effect=_boom,
    ):
        with pytest.raises(RuntimeError, match="acs unreachable"):
            await send_magic_link_email(
                {},  # no job_try key at all
                email="user@example.com",
                raw_token="t",
                click_url_base="http://x",
                acs_connection_string=None,
                acs_sender_address="from@example.com",
            )

    permanent_failure_records = [
        r for r in caplog.records if "magic_link.email.permanent_failure" in r.message
    ]
    assert permanent_failure_records == []
