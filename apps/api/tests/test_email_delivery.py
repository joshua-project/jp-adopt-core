"""Tests for jp_adopt_api.email_delivery — the shared ACS sender.

Lives in jp_adopt_api (not the worker) so the API's drip test-send endpoint can
send without importing the worker package, which the API runtime container does
not ship (that import is why send-test 503'd in production).
"""

from __future__ import annotations

import pytest

from jp_adopt_api.email_delivery import send_via_acs


@pytest.mark.asyncio
async def test_dev_fallback_returns_none_without_acs() -> None:
    # No connection string (dev / unconfigured) → render worked but nothing
    # delivered. Must not raise and must not touch the Azure SDK.
    result = await send_via_acs(
        email="amy@example.com",
        subject="Hi",
        html="<p>x</p>",
        plain="x",
        acs_connection_string=None,
        acs_sender_address="donotreply@joshuaproject.net",
    )
    assert result is None


def test_send_test_endpoint_does_not_import_worker() -> None:
    # Regression: the drips router must reach the ACS sender via jp_adopt_api,
    # never via jp_adopt_worker (absent from the API runtime container).
    import inspect

    from jp_adopt_api.routers import drips

    source = inspect.getsource(drips)
    assert "jp_adopt_worker" not in source
