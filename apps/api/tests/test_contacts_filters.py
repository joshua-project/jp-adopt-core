"""GET /v1/contacts filter params + /v1/contacts/status_counts."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jp_adopt_api.config import get_settings
from jp_adopt_api.models import Contact

os.environ.setdefault("STRICT_AUTH", "false")
os.environ.setdefault("APP_ENV", "development")
get_settings.cache_clear()


# ─── Fixtures ────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as s:
        yield s
    await engine.dispose()


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer dev-local"}


async def _make_contact(
    session: AsyncSession,
    *,
    party_kind: str,
    adopter_status: str | None = None,
    facilitator_status: str | None = None,
    suffix: str | None = None,
) -> Contact:
    """Insert a fresh contact tagged with origin='filter_test' so the
    cleanup helper can scoop them up at the end."""
    contact = Contact(
        id=uuid.uuid4(),
        party_kind=party_kind,
        display_name=f"Filter Test {suffix or uuid.uuid4().hex[:6]}",
        adopter_status=adopter_status,
        facilitator_status=facilitator_status,
        email_normalized=f"filter-{uuid.uuid4().hex[:10]}@example.com",
        origin="filter_test",
    )
    session.add(contact)
    await session.flush()
    await session.commit()
    return contact


async def _cleanup_filter_test_contacts(session: AsyncSession) -> None:
    await session.execute(
        delete(Contact).where(Contact.origin == "filter_test")
    )
    await session.commit()


# ─── Filter tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_party_kind_filter(
    session: AsyncSession,
) -> None:
    """party_kind=adopter returns only adopters; party_kind=facilitator
    only facilitators."""
    await _make_contact(session, party_kind="adopter", adopter_status="new")
    await _make_contact(session, party_kind="adopter", adopter_status="matched")
    await _make_contact(
        session, party_kind="facilitator", facilitator_status="ready"
    )
    try:
        with TestClient(__import__("jp_adopt_api.main", fromlist=["app"]).app) as client:
            r = client.get(
                "/v1/contacts?party_kind=adopter&limit=100",
                headers=_auth_headers(),
            )
            assert r.status_code == 200, r.text
            kinds = {item["party_kind"] for item in r.json()["items"]}
            assert kinds == {"adopter"}, f"unexpected kinds: {kinds}"

            r = client.get(
                "/v1/contacts?party_kind=facilitator&limit=100",
                headers=_auth_headers(),
            )
            kinds = {item["party_kind"] for item in r.json()["items"]}
            assert kinds == {"facilitator"}
    finally:
        await _cleanup_filter_test_contacts(session)


@pytest.mark.asyncio
async def test_adopter_status_multi_filter(
    session: AsyncSession,
) -> None:
    """adopter_status accepts repeated query params; the IN-set semantics
    return rows matching any of them."""
    await _make_contact(session, party_kind="adopter", adopter_status="new")
    await _make_contact(session, party_kind="adopter", adopter_status="matched")
    await _make_contact(
        session, party_kind="adopter", adopter_status="active"
    )
    await _make_contact(
        session, party_kind="adopter", adopter_status="inactive"
    )
    try:
        with TestClient(__import__("jp_adopt_api.main", fromlist=["app"]).app) as client:
            r = client.get(
                "/v1/contacts"
                "?party_kind=adopter"
                "&adopter_status=matched"
                "&adopter_status=active"
                "&limit=100",
                headers=_auth_headers(),
            )
            assert r.status_code == 200, r.text
            statuses = {
                item["adopter_status"] for item in r.json()["items"]
            }
            # All four filter-test rows belong to the filter_test origin,
            # but only the matched + active subset should come back here.
            our_statuses = statuses & {"matched", "active"}
            assert our_statuses == {"matched", "active"}
            assert "new" not in statuses
            assert "inactive" not in statuses
    finally:
        await _cleanup_filter_test_contacts(session)


@pytest.mark.asyncio
async def test_unknown_status_returns_422_with_allowed_list(
    session: AsyncSession,
) -> None:
    """Typos in adopter_status / facilitator_status get a structured 422
    naming the allowed values, rather than silently returning empty."""
    with TestClient(__import__("jp_adopt_api.main", fromlist=["app"]).app) as client:
        r = client.get(
            "/v1/contacts?party_kind=adopter&adopter_status=notarealstatus",
            headers=_auth_headers(),
        )
        assert r.status_code == 422, r.text
        body = r.json()
        assert body["detail"]["code"] == "unknown_adopter_status"
        assert "matched" in body["detail"]["allowed"]
        assert "new" in body["detail"]["allowed"]

        r = client.get(
            "/v1/contacts?party_kind=facilitator&facilitator_status=ghost",
            headers=_auth_headers(),
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "unknown_facilitator_status"


@pytest.mark.asyncio
async def test_filter_total_matches_filtered_count(
    session: AsyncSession,
) -> None:
    """The ``total`` in the list response reflects the filtered count,
    not the unfiltered table size."""
    await _make_contact(session, party_kind="adopter", adopter_status="new")
    await _make_contact(session, party_kind="adopter", adopter_status="new")
    await _make_contact(
        session, party_kind="adopter", adopter_status="matched"
    )
    try:
        with TestClient(__import__("jp_adopt_api.main", fromlist=["app"]).app) as client:
            r = client.get(
                "/v1/contacts?party_kind=adopter&adopter_status=new&limit=100",
                headers=_auth_headers(),
            )
            assert r.status_code == 200
            body = r.json()
            # At least the 2 filter_test rows; may be more if seeded data
            # has 'new' adopters. Just confirm total matches items length
            # when limit isn't reached.
            assert body["total"] == len(body["items"])
            assert body["total"] >= 2
    finally:
        await _cleanup_filter_test_contacts(session)


# ─── status_counts tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_counts_adopter(
    session: AsyncSession,
) -> None:
    """status_counts groups adopter contacts by adopter_status with
    correct counts."""
    await _make_contact(session, party_kind="adopter", adopter_status="new")
    await _make_contact(session, party_kind="adopter", adopter_status="new")
    await _make_contact(
        session, party_kind="adopter", adopter_status="matched"
    )
    await _make_contact(session, party_kind="adopter", adopter_status=None)
    try:
        with TestClient(__import__("jp_adopt_api.main", fromlist=["app"]).app) as client:
            r = client.get(
                "/v1/contacts/status_counts?party_kind=adopter",
                headers=_auth_headers(),
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["party_kind"] == "adopter"
            assert isinstance(body["counts"], dict)
            assert body["total"] >= 4
            # Filter-test rows contributed: 2 new, 1 matched, 1 NULL.
            # Confirm those buckets are at least that large (other tests'
            # seed data may inflate further).
            assert body["counts"].get("new", 0) >= 2
            assert body["counts"].get("matched", 0) >= 1
            assert body["counts"].get("__unset__", 0) >= 1
    finally:
        await _cleanup_filter_test_contacts(session)


@pytest.mark.asyncio
async def test_status_counts_facilitator(
    session: AsyncSession,
) -> None:
    await _make_contact(
        session, party_kind="facilitator", facilitator_status="ready"
    )
    await _make_contact(
        session, party_kind="facilitator", facilitator_status="ready"
    )
    await _make_contact(
        session, party_kind="facilitator", facilitator_status="not_ready"
    )
    try:
        with TestClient(__import__("jp_adopt_api.main", fromlist=["app"]).app) as client:
            r = client.get(
                "/v1/contacts/status_counts?party_kind=facilitator",
                headers=_auth_headers(),
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["party_kind"] == "facilitator"
            assert body["counts"].get("ready", 0) >= 2
            assert body["counts"].get("not_ready", 0) >= 1
    finally:
        await _cleanup_filter_test_contacts(session)


def test_status_counts_requires_party_kind() -> None:
    """party_kind is a required query param — without it, FastAPI returns
    422 (not 500)."""
    with TestClient(__import__("jp_adopt_api.main", fromlist=["app"]).app) as client:
        r = client.get(
            "/v1/contacts/status_counts",
            headers=_auth_headers(),
        )
        assert r.status_code == 422


def test_status_counts_rejects_unknown_party_kind() -> None:
    """party_kind only accepts 'adopter' or 'facilitator'."""
    with TestClient(__import__("jp_adopt_api.main", fromlist=["app"]).app) as client:
        r = client.get(
            "/v1/contacts/status_counts?party_kind=other",
            headers=_auth_headers(),
        )
        assert r.status_code == 422
