"""F3 (#55): /v1/suppression-list endpoint tests.

The drip worker hard-filters sends against ``suppression_list``; staff need
a UI to mutate it. These tests cover the GET/POST/DELETE surface, including
the idempotent POST contract (re-adding returns the existing row at 200, no
duplicate row), pagination, and the role gate.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jp_adopt_api.config import get_settings
from jp_adopt_api.domain.drips import email_hash
from jp_adopt_api.main import app
from jp_adopt_api.models import SuppressionList

os.environ.setdefault("STRICT_AUTH", "false")
os.environ.setdefault("APP_ENV", "development")
get_settings.cache_clear()

AUTH = {"Authorization": "Bearer dev-local"}


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def _fresh_email() -> str:
    return f"suppress-{uuid.uuid4().hex[:10]}@example.com"


def test_post_fresh_email_returns_row(client: TestClient):
    email = _fresh_email()
    r = client.post(
        "/v1/suppression-list",
        headers=AUTH,
        json={"email": email},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email_hash"] == email_hash(email)
    assert body["reason"] == "manual"
    assert body["suppressed_at"]
    # Cleanup via DELETE through the same surface.
    d = client.delete(
        f"/v1/suppression-list/{body['email_hash']}", headers=AUTH
    )
    assert d.status_code == 204


@pytest.mark.asyncio
async def test_post_same_email_twice_is_idempotent(
    client: TestClient, session: AsyncSession
):
    email = _fresh_email()
    h = email_hash(email)
    try:
        r1 = client.post(
            "/v1/suppression-list", headers=AUTH, json={"email": email}
        )
        assert r1.status_code == 200, r1.text
        r2 = client.post(
            "/v1/suppression-list", headers=AUTH, json={"email": email}
        )
        assert r2.status_code == 200, r2.text
        # Same row returned both times.
        assert r1.json()["email_hash"] == r2.json()["email_hash"]
        rows = (
            await session.execute(
                select(SuppressionList).where(SuppressionList.email_hash == h)
            )
        ).scalars().all()
        assert len(rows) == 1
    finally:
        await session.execute(
            delete(SuppressionList).where(SuppressionList.email_hash == h)
        )
        await session.commit()


def test_post_with_explicit_reason_and_metadata_persists_both(
    client: TestClient,
):
    email = _fresh_email()
    r = client.post(
        "/v1/suppression-list",
        headers=AUTH,
        json={
            "email": email,
            "reason": "hard_bounce",
            "source_metadata": {"acs_event_id": "abc-123"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    try:
        assert body["reason"] == "hard_bounce"
        assert body["source_metadata"] == {"acs_event_id": "abc-123"}
    finally:
        client.delete(
            f"/v1/suppression-list/{body['email_hash']}", headers=AUTH
        )


def test_list_paginated_returns_count_and_cap(client: TestClient):
    hashes: list[str] = []
    for _ in range(3):
        r = client.post(
            "/v1/suppression-list",
            headers=AUTH,
            json={"email": _fresh_email()},
        )
        assert r.status_code == 200
        hashes.append(r.json()["email_hash"])
    try:
        r = client.get(
            "/v1/suppression-list?limit=2&offset=0", headers=AUTH
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] >= 3
        assert len(body["items"]) == 2
    finally:
        for h in hashes:
            client.delete(f"/v1/suppression-list/{h}", headers=AUTH)


def test_delete_existing_hash_returns_204_and_removes(
    client: TestClient,
):
    email = _fresh_email()
    h = email_hash(email)
    add = client.post(
        "/v1/suppression-list", headers=AUTH, json={"email": email}
    )
    assert add.status_code == 200, add.text
    r = client.delete(f"/v1/suppression-list/{h}", headers=AUTH)
    assert r.status_code == 204, r.text
    # No longer findable.
    r2 = client.get(
        f"/v1/suppression-list?limit=200&offset=0", headers=AUTH
    )
    assert h not in [i["email_hash"] for i in r2.json()["items"]]


def test_delete_unknown_hash_returns_404(client: TestClient):
    bogus = "f" * 64
    r = client.delete(f"/v1/suppression-list/{bogus}", headers=AUTH)
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["code"] == "suppression_not_found"


@pytest.mark.parametrize(
    "verb,path,payload",
    [
        ("GET", "/v1/suppression-list", None),
        ("POST", "/v1/suppression-list", {"email": "x@example.com"}),
        ("DELETE", "/v1/suppression-list/" + ("a" * 64), None),
    ],
)
@pytest.mark.asyncio
async def test_non_staff_role_returns_403_on_all_verbs(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
    path: str,
    payload: dict | None,
):
    from jp_adopt_api import deps as deps_module

    async def _fake_roles(db: object, user_sub: str) -> frozenset[str]:
        return frozenset({"facilitator"})

    monkeypatch.setattr(deps_module, "load_user_roles", _fake_roles)
    request = client.request(verb, path, headers=AUTH, json=payload)
    assert request.status_code == 403, request.text
