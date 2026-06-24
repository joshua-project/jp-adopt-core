"""HTTP tests for the drip body editor (U4): body_html on create/patch,
send-test endpoint, and the merge-tokens list.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jp_adopt_api.config import get_settings
from jp_adopt_api.main import app
from jp_adopt_api.models import Campaign

os.environ.setdefault("STRICT_AUTH", "false")
os.environ.setdefault("APP_ENV", "development")
get_settings.cache_clear()


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def _auth_headers(token: str = "dev-local") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _delete_campaign(session: AsyncSession, campaign_id: str) -> None:
    campaign = await session.get(Campaign, uuid.UUID(campaign_id))
    if campaign:
        await session.delete(campaign)  # cascade removes steps
        await session.commit()


def _make_campaign(client: TestClient) -> str:
    r = client.post(
        "/v1/drips/campaigns",
        json={
            "name": "Body Editor Test",
            "trigger_type": "manual",
        },
        headers=_auth_headers(),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_create_step_with_body_html_sanitizes_and_keeps_token(
    client: TestClient, session: AsyncSession
) -> None:
    campaign_id = _make_campaign(client)
    try:
        r = client.post(
            f"/v1/drips/campaigns/{campaign_id}/steps",
            json={
                "position": 0,
                "subject": "Hi",
                "body_html": (
                    "<p>Hello {{ contact_display_name }}</p>"
                    "<script>alert(1)</script>"
                ),
            },
            headers=_auth_headers(),
        )
        assert r.status_code == 201, r.text
        body = r.json()["body_html"]
        assert "<script" not in body
        assert "{{ contact_display_name }}" in body
        assert r.json()["mjml_template_name"] is None
    finally:
        await _delete_campaign(session, campaign_id)


@pytest.mark.asyncio
async def test_create_step_requires_body_or_template(
    client: TestClient, session: AsyncSession
) -> None:
    campaign_id = _make_campaign(client)
    try:
        r = client.post(
            f"/v1/drips/campaigns/{campaign_id}/steps",
            json={"position": 0, "subject": "Hi"},
            headers=_auth_headers(),
        )
        assert r.status_code == 422, r.text
    finally:
        await _delete_campaign(session, campaign_id)


@pytest.mark.asyncio
async def test_patch_step_body_html_is_sanitized(
    client: TestClient, session: AsyncSession
) -> None:
    campaign_id = _make_campaign(client)
    try:
        client.post(
            f"/v1/drips/campaigns/{campaign_id}/steps",
            json={
                "position": 0,
                "subject": "Hi",
                "mjml_template_name": "facilitator-welcome.step-0.mjml",
            },
            headers=_auth_headers(),
        )
        r = client.patch(
            f"/v1/drips/campaigns/{campaign_id}/steps/0",
            json={"body_html": "<p>New body</p><script>x</script>"},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        assert r.json()["body_html"] == "<p>New body</p>"
    finally:
        await _delete_campaign(session, campaign_id)


@pytest.mark.asyncio
async def test_send_test_defaults_to_caller_email(
    client: TestClient, session: AsyncSession
) -> None:
    campaign_id = _make_campaign(client)
    try:
        client.post(
            f"/v1/drips/campaigns/{campaign_id}/steps",
            json={
                "position": 0,
                "subject": "Hi",
                "body_html": "<p>Hello {{ contact_display_name }}</p>",
            },
            headers=_auth_headers(),
        )
        r = client.post(
            f"/v1/drips/campaigns/{campaign_id}/steps/0/send-test",
            json={},
            headers=_auth_headers(),
        )
        assert r.status_code == 202, r.text
        # dev-local resolves to dev@local.invalid in the auth layer.
        assert "@" in r.json()["to_email"]
    finally:
        await _delete_campaign(session, campaign_id)


@pytest.mark.asyncio
async def test_send_test_uses_explicit_recipient(
    client: TestClient, session: AsyncSession
) -> None:
    campaign_id = _make_campaign(client)
    try:
        client.post(
            f"/v1/drips/campaigns/{campaign_id}/steps",
            json={
                "position": 0,
                "subject": "Hi",
                "body_html": "<p>Hello {{ contact_display_name }}</p>",
            },
            headers=_auth_headers(),
        )
        r = client.post(
            f"/v1/drips/campaigns/{campaign_id}/steps/0/send-test",
            json={"to_email": "amy@example.com"},
            headers=_auth_headers(),
        )
        assert r.status_code == 202, r.text
        assert r.json()["to_email"] == "amy@example.com"
    finally:
        await _delete_campaign(session, campaign_id)


def test_merge_tokens_lists_contact_display_name(client: TestClient) -> None:
    r = client.get("/v1/drips/merge-tokens", headers=_auth_headers())
    assert r.status_code == 200, r.text
    names = {t["name"] for t in r.json()["items"]}
    assert "contact_display_name" in names
