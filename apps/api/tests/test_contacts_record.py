"""Contact record (U1/U2): per-contact read aggregates + add-note.

Covers GET /v1/contacts/{id}/{matches,transitions,activity,timeline} and
POST /v1/contacts/{id}/activity. Runs against the local Postgres (see
conftest). Rows are tagged origin='record_test' for cleanup.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

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
from jp_adopt_api.models import (
    ActivityLog,
    AdopterInterest,
    Contact,
    ContactProfile,
    FacilitatingOrg,
    Match,
    TransitionAudit,
)

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


@pytest_asyncio.fixture
async def fixture_contact(session: AsyncSession) -> AsyncIterator[Contact]:
    """An adopter with one interest, one match, one transition, one note."""
    org = FacilitatingOrg(
        id=uuid.uuid4(), name=f"RecordTest Org {uuid.uuid4().hex[:6]}"
    )
    contact = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name="Record Test Adopter",
        adopter_status="new",
        email_normalized=f"record-{uuid.uuid4().hex[:10]}@example.com",
        origin="record_test",
    )
    session.add_all([org, contact])
    await session.flush()
    interest = AdopterInterest(id=uuid.uuid4(), contact_id=contact.id, people_id3=None)
    session.add(interest)
    await session.flush()
    session.add_all(
        [
            Match(
                id=uuid.uuid4(),
                adopter_interest_id=interest.id,
                facilitator_org_id=org.id,
                status="recommended",
            ),
            TransitionAudit(
                id=uuid.uuid4(),
                contact_id=contact.id,
                from_state="draft",
                to_state="new",
                actor_id="dev-local",
                actor_role="staff_admin",
            ),
            ActivityLog(
                id=uuid.uuid4(),
                contact_id=contact.id,
                author_id="dev-local",
                body="seeded record note",
                kind="note",
                occurred_at=datetime.now(UTC),
            ),
        ]
    )
    await session.commit()
    yield contact
    # Cleanup: transition_audit has no ON DELETE CASCADE, so delete it first;
    # deleting the contact cascades interest -> match and activity_log.
    await session.execute(
        delete(TransitionAudit).where(TransitionAudit.contact_id == contact.id)
    )
    await session.execute(delete(Contact).where(Contact.origin == "record_test"))
    await session.execute(delete(FacilitatingOrg).where(FacilitatingOrg.id == org.id))
    await session.commit()


def test_matches_returns_contact_matches(client: TestClient, fixture_contact: Contact):
    r = client.get(f"/v1/contacts/{fixture_contact.id}/matches", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "recommended"
    assert body["items"][0]["facilitator_name"].startswith("RecordTest Org")


def test_transitions_newest_first(client: TestClient, fixture_contact: Contact):
    r = client.get(f"/v1/contacts/{fixture_contact.id}/transitions", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["to_state"] == "new"
    assert body["items"][0]["from_state"] == "draft"


def test_activity_lists_notes(client: TestClient, fixture_contact: Contact):
    r = client.get(f"/v1/contacts/{fixture_contact.id}/activity", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["body"] == "seeded record note"


def test_timeline_merges_sources(client: TestClient, fixture_contact: Contact):
    r = client.get(f"/v1/contacts/{fixture_contact.id}/timeline", headers=AUTH)
    assert r.status_code == 200, r.text
    types = {e["type"] for e in r.json()["items"]}
    assert types == {"transition", "match", "activity"}


def test_add_note_persists(client: TestClient, fixture_contact: Contact):
    r = client.post(
        f"/v1/contacts/{fixture_contact.id}/activity",
        headers=AUTH,
        json={"body": "a fresh staff note"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["body"] == "a fresh staff note"
    # It now shows up in the read endpoint (2 total: seeded + this one).
    r2 = client.get(f"/v1/contacts/{fixture_contact.id}/activity", headers=AUTH)
    assert r2.json()["total"] == 2


def test_add_note_rejects_empty(client: TestClient, fixture_contact: Contact):
    r = client.post(
        f"/v1/contacts/{fixture_contact.id}/activity", headers=AUTH, json={"body": ""}
    )
    assert r.status_code == 422


def test_unknown_contact_404(client: TestClient):
    missing = uuid.uuid4()
    for path in ("matches", "transitions", "activity", "timeline"):
        r = client.get(f"/v1/contacts/{missing}/{path}", headers=AUTH)
        assert r.status_code == 404, f"{path}: {r.status_code}"


# ── F3: send-email endpoint ────────────────────────────────────────────────


def test_send_email_to_adopter_returns_recipient_and_queues(
    client: TestClient, fixture_contact: Contact
):
    r = client.post(
        f"/v1/contacts/{fixture_contact.id}/emails",
        headers=AUTH,
        json={"subject": "Hello", "body": "Welcome to the program."},
    )
    assert r.status_code == 202, r.text
    data = r.json()
    assert data["to"] == [fixture_contact.email_normalized]
    assert data["status"] == "queued"
    assert data["note_id"]
    # Shows up as an `email` note on the activity timeline.
    act = client.get(f"/v1/contacts/{fixture_contact.id}/activity", headers=AUTH)
    assert "email" in [i["kind"] for i in act.json()["items"]]


@pytest.mark.asyncio
async def test_send_email_records_subject_and_recipients_in_metadata(
    client: TestClient, fixture_contact: Contact, session: AsyncSession
):
    r = client.post(
        f"/v1/contacts/{fixture_contact.id}/emails",
        headers=AUTH,
        json={"subject": "Subject X", "body": "Body Y"},
    )
    assert r.status_code == 202, r.text
    note = await session.get(ActivityLog, uuid.UUID(r.json()["note_id"]))
    assert note is not None
    assert note.kind == "email"
    assert note.body == "Body Y"
    assert note.source_metadata["subject"] == "Subject X"
    assert note.source_metadata["to"] == [fixture_contact.email_normalized]
    # Background dev-fallback send flips queued -> logged by the time the
    # TestClient returns; accept either to avoid coupling to task timing.
    assert note.source_metadata["status"] in ("queued", "logged")


@pytest.mark.asyncio
async def test_send_email_facilitator_includes_secondary(
    client: TestClient, session: AsyncSession
):
    contact = Contact(
        id=uuid.uuid4(),
        party_kind="facilitator",
        display_name="Fac With Secondary",
        facilitator_status="ready",
        email_normalized=f"fac-{uuid.uuid4().hex[:10]}@example.com",
        origin="record_test",
    )
    session.add(contact)
    await session.flush()
    session.add(
        ContactProfile(
            id=uuid.uuid4(),
            contact_id=contact.id,
            secondary_contact_email="second@example.com",
        )
    )
    await session.commit()
    try:
        r = client.post(
            f"/v1/contacts/{contact.id}/emails",
            headers=AUTH,
            json={"subject": "s", "body": "b", "include_secondary": True},
        )
        assert r.status_code == 202, r.text
        assert set(r.json()["to"]) == {
            contact.email_normalized,
            "second@example.com",
        }
    finally:
        await session.execute(
            delete(ActivityLog).where(ActivityLog.contact_id == contact.id)
        )
        await session.execute(delete(Contact).where(Contact.id == contact.id))
        await session.commit()


def test_send_email_include_secondary_ignored_for_adopter(
    client: TestClient, fixture_contact: Contact
):
    # fixture_contact is an adopter — it has no secondary contact, so the
    # flag is a no-op.
    r = client.post(
        f"/v1/contacts/{fixture_contact.id}/emails",
        headers=AUTH,
        json={"subject": "s", "body": "b", "include_secondary": True},
    )
    assert r.status_code == 202, r.text
    assert r.json()["to"] == [fixture_contact.email_normalized]


@pytest.mark.asyncio
async def test_send_email_no_email_returns_422(
    client: TestClient, session: AsyncSession
):
    contact = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name="No Email",
        adopter_status="new",
        email_normalized=None,
        origin="record_test",
    )
    session.add(contact)
    await session.commit()
    try:
        r = client.post(
            f"/v1/contacts/{contact.id}/emails",
            headers=AUTH,
            json={"subject": "s", "body": "b"},
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "email_required"
    finally:
        await session.execute(delete(Contact).where(Contact.id == contact.id))
        await session.commit()


def test_send_email_unknown_contact_404(client: TestClient):
    r = client.post(
        f"/v1/contacts/{uuid.uuid4()}/emails",
        headers=AUTH,
        json={"subject": "s", "body": "b"},
    )
    assert r.status_code == 404, r.text
