"""U11 manual contact create endpoint tests."""

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
from jp_adopt_api.main import app
from jp_adopt_api.models import (
    AdopterInterest,
    Contact,
    FacilitatingOrg,
    FacilitatorFpgCoverage,
    Fpg,
    Match,
    MatchAttempt,
    Outbox,
    TransitionAudit,
)

os.environ.setdefault("STRICT_AUTH", "false")
os.environ.setdefault("APP_ENV", "development")
get_settings.cache_clear()


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


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer dev-local"}


async def _ensure_fpg(session: AsyncSession, people_id3: str) -> None:
    existing = await session.get(Fpg, people_id3)
    if existing is None:
        session.add(Fpg(people_id3=people_id3, name=f"Test {people_id3}", country_code="US"))
        await session.flush()
        await session.commit()


async def _cleanup_by_email(session: AsyncSession, email: str) -> None:
    contact = (
        await session.execute(
            select(Contact).where(Contact.email_normalized == email)
        )
    ).scalar_one_or_none()
    if contact is None:
        return
    interest_ids = (
        await session.execute(
            select(AdopterInterest.id).where(
                AdopterInterest.contact_id == contact.id
            )
        )
    ).scalars().all()
    if interest_ids:
        await session.execute(
            delete(MatchAttempt).where(
                MatchAttempt.adopter_interest_id.in_(interest_ids)
            )
        )
        await session.execute(
            delete(Match).where(Match.adopter_interest_id.in_(interest_ids))
        )
        await session.execute(
            delete(AdopterInterest).where(AdopterInterest.id.in_(interest_ids))
        )
    await session.execute(
        delete(TransitionAudit).where(TransitionAudit.contact_id == contact.id)
    )
    await session.execute(delete(Contact).where(Contact.id == contact.id))
    await session.commit()


async def _make_org(session: AsyncSession, *, active: bool = True) -> FacilitatingOrg:
    org = FacilitatingOrg(
        id=uuid.uuid4(),
        name=f"Test Org {uuid.uuid4().hex[:6]}",
        country_code="US",
        capacity_total=5,
        capacity_committed=0,
        active=active,
        is_triage_org=False,
    )
    session.add(org)
    await session.flush()
    await session.commit()
    return org


# ─── happy paths ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_manual_create_minimal_adopter_with_one_rop3(
    client: TestClient, session: AsyncSession
) -> None:
    email = f"manual-{uuid.uuid4().hex[:8]}@example.com"
    await _ensure_fpg(session, "AAA01")
    try:
        r = client.post(
            "/v1/contacts/manual",
            json={
                "display_name": "Alice",
                "email": email,
                "party_kind": "adopter",
                "fpg_people_id3s": ["AAA01"],
            },
            headers=_auth_headers(),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["created"] is True
        assert len(body["interest_ids"]) == 1
        assert body["match_id"] is None
        assert body["contact_status"] == "new"

        # Confirm contact row exists with origin=manual_entry
        contact = (
            await session.execute(
                select(Contact).where(Contact.email_normalized == email)
            )
        ).scalar_one()
        assert contact.origin == "manual_entry"
        assert contact.adopter_status == "new"

        # Outbox event was emitted
        outbox = (
            await session.execute(
                select(Outbox).where(
                    Outbox.event_type == "jp.adopt.v1.contact.manual_created"
                )
            )
        ).scalars().all()
        assert any(
            o.payload_json.get("contact_id") == str(contact.id) for o in outbox
        )
    finally:
        await _cleanup_by_email(session, email)


@pytest.mark.asyncio
async def test_manual_create_no_fpg_becomes_potential_adopter(
    client: TestClient, session: AsyncSession
) -> None:
    email = f"manual-{uuid.uuid4().hex[:8]}@example.com"
    try:
        r = client.post(
            "/v1/contacts/manual",
            json={
                "display_name": "No FPG",
                "email": email,
                "party_kind": "adopter",
            },
            headers=_auth_headers(),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        # No fpg_people_id3s ⇒ single AdopterInterest with people_id3=NULL
        assert len(body["interest_ids"]) == 1
        assert body["contact_status"] == "potential_adopter"
        # The interest row really has people_id3=NULL
        interest = (
            await session.execute(
                select(AdopterInterest).where(
                    AdopterInterest.id == uuid.UUID(body["interest_ids"][0])
                )
            )
        ).scalar_one()
        assert interest.people_id3 is None
    finally:
        await _cleanup_by_email(session, email)


@pytest.mark.asyncio
async def test_manual_create_with_facilitator_creates_match(
    client: TestClient, session: AsyncSession
) -> None:
    email = f"manual-{uuid.uuid4().hex[:8]}@example.com"
    await _ensure_fpg(session, "AAA02")
    org = await _make_org(session)
    session.add(FacilitatorFpgCoverage(facilitator_org_id=org.id, people_id3="AAA02"))
    await session.commit()
    try:
        r = client.post(
            "/v1/contacts/manual",
            json={
                "display_name": "Pre-matched",
                "email": email,
                "party_kind": "adopter",
                "fpg_people_id3s": ["AAA02"],
                "facilitator_org_id": str(org.id),
            },
            headers=_auth_headers(),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["match_id"] is not None
        match = await session.get(Match, uuid.UUID(body["match_id"]))
        assert match is not None
        assert match.facilitator_org_id == org.id
        assert match.status == "recommended"
    finally:
        await _cleanup_by_email(session, email)
        await session.execute(
            delete(FacilitatorFpgCoverage).where(
                FacilitatorFpgCoverage.facilitator_org_id == org.id
            )
        )
        await session.execute(
            delete(FacilitatingOrg).where(FacilitatingOrg.id == org.id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_manual_create_reuses_existing_contact_by_email(
    client: TestClient, session: AsyncSession
) -> None:
    email = f"manual-{uuid.uuid4().hex[:8]}@example.com"
    try:
        # First create
        r = client.post(
            "/v1/contacts/manual",
            json={"display_name": "First", "email": email, "party_kind": "adopter"},
            headers=_auth_headers(),
        )
        assert r.status_code == 201, r.text
        first_contact_id = r.json()["contact_id"]
        assert r.json()["created"] is True

        # Second create with same email
        r = client.post(
            "/v1/contacts/manual",
            json={"display_name": "Second", "email": email, "party_kind": "adopter"},
            headers=_auth_headers(),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["created"] is False
        assert body["contact_id"] == first_contact_id
    finally:
        await _cleanup_by_email(session, email)


# ─── error paths ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_manual_create_unknown_rop3_returns_422(
    client: TestClient, session: AsyncSession
) -> None:
    email = f"manual-{uuid.uuid4().hex[:8]}@example.com"
    try:
        r = client.post(
            "/v1/contacts/manual",
            json={
                "display_name": "Bad rop3",
                "email": email,
                "party_kind": "adopter",
                "fpg_people_id3s": ["DOES_NOT_EXIST"],
            },
            headers=_auth_headers(),
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "unknown_fpg_people_id3"
    finally:
        await _cleanup_by_email(session, email)


@pytest.mark.asyncio
async def test_manual_create_unknown_origin_returns_400(
    client: TestClient,
) -> None:
    r = client.post(
        "/v1/contacts/manual",
        json={
            "display_name": "Bad origin",
            "email": f"manual-{uuid.uuid4().hex[:8]}@example.com",
            "origin": "bad_value",
        },
        headers=_auth_headers(),
    )
    # Pydantic ValidationError → sanitized 422 from main.py
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_manual_create_inactive_facilitator_returns_409(
    client: TestClient, session: AsyncSession
) -> None:
    email = f"manual-{uuid.uuid4().hex[:8]}@example.com"
    org = await _make_org(session, active=False)
    try:
        r = client.post(
            "/v1/contacts/manual",
            json={
                "display_name": "Inactive fac",
                "email": email,
                "facilitator_org_id": str(org.id),
            },
            headers=_auth_headers(),
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "facilitator_inactive"
    finally:
        await _cleanup_by_email(session, email)
        await session.execute(
            delete(FacilitatingOrg).where(FacilitatingOrg.id == org.id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_manual_create_unauthenticated_returns_401(
    client: TestClient,
) -> None:
    r = client.post(
        "/v1/contacts/manual",
        json={
            "display_name": "No auth",
            "email": "x@example.com",
        },
    )
    assert r.status_code == 401
