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
    display_name: str | None = None,
    email_normalized: str | None = None,
) -> Contact:
    """Insert a fresh contact tagged with origin='filter_test' so the
    cleanup helper can scoop them up at the end."""
    contact = Contact(
        id=uuid.uuid4(),
        party_kind=party_kind,
        display_name=display_name
        if display_name is not None
        else f"Filter Test {suffix or uuid.uuid4().hex[:6]}",
        adopter_status=adopter_status,
        facilitator_status=facilitator_status,
        email_normalized=email_normalized
        if email_normalized is not None
        else f"filter-{uuid.uuid4().hex[:10]}@example.com",
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


# ─── q search tests (U1) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_q_matches_name_or_email(
    session: AsyncSession,
) -> None:
    """q returns contacts whose display_name contains the term (case-
    insensitive) AND contacts whose email contains it; non-matches are
    excluded."""
    by_name = await _make_contact(
        session,
        party_kind="adopter",
        adopter_status="new",
        display_name="Filter Test Jane Doe",
        email_normalized=f"unrelated-{uuid.uuid4().hex[:10]}@example.com",
    )
    by_email = await _make_contact(
        session,
        party_kind="adopter",
        adopter_status="new",
        display_name="Filter Test Bob Smith",
        email_normalized=f"jane-{uuid.uuid4().hex[:10]}@example.com",
    )
    nonmatch = await _make_contact(
        session,
        party_kind="adopter",
        adopter_status="new",
        display_name="Filter Test Carol King",
        email_normalized=f"carol-{uuid.uuid4().hex[:10]}@example.com",
    )
    try:
        with TestClient(
            __import__("jp_adopt_api.main", fromlist=["app"]).app
        ) as client:
            # Case-insensitive: search uppercase, names/emails are mixed case.
            r = client.get(
                "/v1/contacts?q=JANE&limit=200",
                headers=_auth_headers(),
            )
            assert r.status_code == 200, r.text
            ids = {item["id"] for item in r.json()["items"]}
            assert str(by_name.id) in ids
            assert str(by_email.id) in ids
            assert str(nonmatch.id) not in ids
    finally:
        await _cleanup_filter_test_contacts(session)


@pytest.mark.asyncio
async def test_q_filters_across_pages(
    session: AsyncSession,
) -> None:
    """A q match on a contact that would land on 'page 2' is returned at
    offset 0 — proves SQL-level filtering, not page-local filtering."""
    target_token = uuid.uuid4().hex[:8]
    # Create several decoy contacts that do NOT match the token, plus one
    # that does. With a small limit, the matching one would otherwise be
    # paged out.
    for _ in range(5):
        await _make_contact(
            session,
            party_kind="adopter",
            adopter_status="new",
            display_name="Filter Test Decoy",
        )
    target = await _make_contact(
        session,
        party_kind="adopter",
        adopter_status="new",
        display_name=f"Filter Test Needle {target_token}",
    )
    try:
        with TestClient(
            __import__("jp_adopt_api.main", fromlist=["app"]).app
        ) as client:
            r = client.get(
                f"/v1/contacts?q={target_token}&limit=2&offset=0",
                headers=_auth_headers(),
            )
            assert r.status_code == 200, r.text
            body = r.json()
            ids = {item["id"] for item in body["items"]}
            assert str(target.id) in ids
            assert body["total"] == 1
    finally:
        await _cleanup_filter_test_contacts(session)


@pytest.mark.asyncio
async def test_q_combines_with_party_kind_and_status(
    session: AsyncSession,
) -> None:
    """q combines with party_kind/status filters using AND semantics."""
    token = uuid.uuid4().hex[:8]
    wanted = await _make_contact(
        session,
        party_kind="adopter",
        adopter_status="matched",
        display_name=f"Filter Test {token} Wanted",
    )
    # Same token but wrong status — excluded by AND.
    await _make_contact(
        session,
        party_kind="adopter",
        adopter_status="new",
        display_name=f"Filter Test {token} WrongStatus",
    )
    # Same token but wrong party kind — excluded by AND.
    await _make_contact(
        session,
        party_kind="facilitator",
        facilitator_status="ready",
        display_name=f"Filter Test {token} WrongKind",
    )
    try:
        with TestClient(
            __import__("jp_adopt_api.main", fromlist=["app"]).app
        ) as client:
            r = client.get(
                "/v1/contacts"
                "?party_kind=adopter"
                "&adopter_status=matched"
                f"&q={token}"
                "&limit=200",
                headers=_auth_headers(),
            )
            assert r.status_code == 200, r.text
            body = r.json()
            ids = {item["id"] for item in body["items"]}
            assert ids == {str(wanted.id)}
            assert body["total"] == 1
    finally:
        await _cleanup_filter_test_contacts(session)


@pytest.mark.asyncio
async def test_q_escapes_sql_wildcards(
    session: AsyncSession,
) -> None:
    """A literal % or _ in q is matched literally (escaped), not treated
    as a SQL wildcard that would match everything."""
    token = uuid.uuid4().hex[:8]
    literal = await _make_contact(
        session,
        party_kind="adopter",
        adopter_status="new",
        display_name=f"Filter Test {token}_pct%end",
    )
    # A contact that would match if '%' / '_' were treated as wildcards but
    # does NOT contain the literal sequence.
    decoy = await _make_contact(
        session,
        party_kind="adopter",
        adopter_status="new",
        display_name=f"Filter Test {token}XpctYend",
    )
    try:
        with TestClient(
            __import__("jp_adopt_api.main", fromlist=["app"]).app
        ) as client:
            # Search for the literal "_pct%end"; only `literal` contains it.
            r = client.get(
                f"/v1/contacts?q={token}_pct%25end&limit=200",
                headers=_auth_headers(),
            )
            assert r.status_code == 200, r.text
            ids = {item["id"] for item in r.json()["items"]}
            assert str(literal.id) in ids
            assert str(decoy.id) not in ids
    finally:
        await _cleanup_filter_test_contacts(session)


@pytest.mark.asyncio
async def test_q_empty_or_whitespace_is_absent(
    session: AsyncSession,
) -> None:
    """Empty/whitespace q behaves identically to no q (full list)."""
    await _make_contact(session, party_kind="adopter", adopter_status="new")
    await _make_contact(session, party_kind="adopter", adopter_status="new")
    try:
        with TestClient(
            __import__("jp_adopt_api.main", fromlist=["app"]).app
        ) as client:
            base = client.get(
                "/v1/contacts?limit=500",
                headers=_auth_headers(),
            )
            assert base.status_code == 200, base.text
            empty = client.get(
                "/v1/contacts?q=&limit=500",
                headers=_auth_headers(),
            )
            assert empty.status_code == 200, empty.text
            whitespace = client.get(
                "/v1/contacts?q=%20%20%20&limit=500",
                headers=_auth_headers(),
            )
            assert whitespace.status_code == 200, whitespace.text
            assert empty.json()["total"] == base.json()["total"]
            assert whitespace.json()["total"] == base.json()["total"]
    finally:
        await _cleanup_filter_test_contacts(session)


@pytest.mark.asyncio
async def test_q_total_reflects_filtered_count(
    session: AsyncSession,
) -> None:
    """total reflects the filtered count, not the table size."""
    token = uuid.uuid4().hex[:8]
    await _make_contact(
        session,
        party_kind="adopter",
        adopter_status="new",
        display_name=f"Filter Test {token} A",
    )
    await _make_contact(
        session,
        party_kind="adopter",
        adopter_status="new",
        display_name=f"Filter Test {token} B",
    )
    # An unrelated contact that must NOT count toward the filtered total.
    await _make_contact(session, party_kind="adopter", adopter_status="new")
    try:
        with TestClient(
            __import__("jp_adopt_api.main", fromlist=["app"]).app
        ) as client:
            r = client.get(
                f"/v1/contacts?q={token}&limit=200",
                headers=_auth_headers(),
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["total"] == 2
            assert len(body["items"]) == 2
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


@pytest.mark.asyncio
async def test_created_date_filter_and_newest_first(
    session: AsyncSession,
) -> None:
    """created_after / created_before scope by creation date, and results
    come back newest-first."""
    from datetime import UTC, datetime

    from sqlalchemy import update

    app = __import__("jp_adopt_api.main", fromlist=["app"]).app
    old = await _make_contact(
        session, party_kind="adopter", adopter_status="new", display_name="Old One"
    )
    mid = await _make_contact(
        session, party_kind="adopter", adopter_status="new", display_name="Mid One"
    )
    new = await _make_contact(
        session, party_kind="adopter", adopter_status="new", display_name="New One"
    )
    await session.execute(
        update(Contact).where(Contact.id == old.id).values(
            created_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
    )
    await session.execute(
        update(Contact).where(Contact.id == mid.id).values(
            created_at=datetime(2026, 6, 1, tzinfo=UTC)
        )
    )
    await session.execute(
        update(Contact).where(Contact.id == new.id).values(
            created_at=datetime(2026, 6, 20, tzinfo=UTC)
        )
    )
    await session.commit()
    try:
        with TestClient(app) as client:
            r = client.get(
                "/v1/contacts?created_after=2026-05-01&limit=500",
                headers=_auth_headers(),
            )
            assert r.status_code == 200, r.text
            ours = [
                i["display_name"]
                for i in r.json()["items"]
                if i["display_name"] in {"Old One", "Mid One", "New One"}
            ]
            assert "Old One" not in ours
            assert ours == ["New One", "Mid One"]  # newest first

            r2 = client.get(
                "/v1/contacts?created_before=2026-05-01&limit=500",
                headers=_auth_headers(),
            )
            ours2 = [
                i["display_name"]
                for i in r2.json()["items"]
                if i["display_name"] in {"Old One", "Mid One", "New One"}
            ]
            assert ours2 == ["Old One"]
    finally:
        await _cleanup_filter_test_contacts(session)
