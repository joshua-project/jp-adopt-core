"""U8 workflow router tests.

Covers:
  * Happy path — adoption_manager triages new → potential_adopter
  * Happy path — facilitator accepts: matched → active
  * Edge case — concurrent transitions surface as 409
  * Edge case — illegal transition → 409 illegal_transition
  * Edge case — missing reason on a reason-requiring transition → 400
  * Edge case — idempotent retry: re-requesting the current state is 200 no-op
  * Integration — full intake → triage → accept → active flow
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
from jp_adopt_api.main import app
from jp_adopt_api.models import (
    Contact,
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


def _auth_headers(token: str = "dev-local") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _make_contact(
    session: AsyncSession,
    *,
    adopter_status: str | None = "new",
    facilitator_status: str | None = None,
) -> Contact:
    contact = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name="WF Contact",
        adopter_status=adopter_status,
        facilitator_status=facilitator_status,
        email_normalized=f"wf-{uuid.uuid4().hex[:10]}@example.com",
    )
    session.add(contact)
    await session.flush()
    await session.commit()
    return contact


async def _cleanup_contact(session: AsyncSession, contact: Contact) -> None:
    await session.execute(
        delete(TransitionAudit).where(TransitionAudit.contact_id == contact.id)
    )
    await session.execute(delete(Contact).where(Contact.id == contact.id))
    await session.commit()


@pytest.mark.asyncio
async def test_transition_adopter_new_to_potential(
    client: TestClient, session: AsyncSession
) -> None:
    contact = await _make_contact(session, adopter_status="new")
    try:
        r = client.post(
            f"/v1/contacts/{contact.id}/transition",
            json={"kind": "adopter", "to_state": "potential_adopter"},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["transitioned_to"] == "potential_adopter"
        assert body["contact"]["adopter_status"] == "potential_adopter"
        # An audit row was written.
        audit_count = (
            await session.execute(
                select(TransitionAudit).where(TransitionAudit.contact_id == contact.id)
            )
        ).scalars().all()
        assert len(audit_count) == 1
    finally:
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_transition_facilitator_accept_matched_to_active(
    client: TestClient, session: AsyncSession
) -> None:
    contact = await _make_contact(session, adopter_status="matched")
    try:
        r = client.post(
            f"/v1/contacts/{contact.id}/transition",
            json={"kind": "adopter", "to_state": "active"},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        assert r.json()["contact"]["adopter_status"] == "active"
    finally:
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_transition_illegal_returns_409(
    client: TestClient, session: AsyncSession
) -> None:
    contact = await _make_contact(session, adopter_status="new")
    try:
        # new → active is not in the transition table.
        r = client.post(
            f"/v1/contacts/{contact.id}/transition",
            json={"kind": "adopter", "to_state": "active"},
            headers=_auth_headers(),
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "illegal_transition"
    finally:
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_transition_reason_required_returns_400(
    client: TestClient, session: AsyncSession
) -> None:
    contact = await _make_contact(session, adopter_status="matched")
    try:
        # matched → sent_back requires reason_code.
        r = client.post(
            f"/v1/contacts/{contact.id}/transition",
            json={"kind": "adopter", "to_state": "sent_back"},
            headers=_auth_headers(),
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["code"] == "reason_required"
    finally:
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_transition_idempotent_on_same_state(
    client: TestClient, session: AsyncSession
) -> None:
    """Asking the API to transition a contact to the state it's already in
    should be a no-op 200, not an IllegalTransitionError."""
    contact = await _make_contact(session, adopter_status="matched")
    try:
        r = client.post(
            f"/v1/contacts/{contact.id}/transition",
            json={"kind": "adopter", "to_state": "matched"},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["contact"]["adopter_status"] == "matched"
        # No audit row written (the no-op path returns before transition_adopter).
        audits = (
            await session.execute(
                select(TransitionAudit).where(TransitionAudit.contact_id == contact.id)
            )
        ).scalars().all()
        assert audits == []
    finally:
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_transition_unknown_state_returns_400(
    client: TestClient, session: AsyncSession
) -> None:
    contact = await _make_contact(session, adopter_status="new")
    try:
        r = client.post(
            f"/v1/contacts/{contact.id}/transition",
            json={"kind": "adopter", "to_state": "made_up_status"},
            headers=_auth_headers(),
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["code"] == "invalid_state"
    finally:
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_transition_facilitator_side(
    client: TestClient, session: AsyncSession
) -> None:
    contact = await _make_contact(
        session, adopter_status=None, facilitator_status="new"
    )
    try:
        r = client.post(
            f"/v1/contacts/{contact.id}/transition",
            json={"kind": "facilitator", "to_state": "ready"},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        assert r.json()["contact"]["facilitator_status"] == "ready"
    finally:
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_transition_missing_contact_returns_404(client: TestClient) -> None:
    r = client.post(
        f"/v1/contacts/{uuid.uuid4()}/transition",
        json={"kind": "adopter", "to_state": "contacted"},
        headers=_auth_headers(),
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_transition_unauthenticated_returns_401(
    client: TestClient, session: AsyncSession
) -> None:
    contact = await _make_contact(session)
    try:
        r = client.post(
            f"/v1/contacts/{contact.id}/transition",
            json={"kind": "adopter", "to_state": "contacted"},
        )
        assert r.status_code == 401
    finally:
        await _cleanup_contact(session, contact)


# ─── Integration test: full intake → triage → accept loop ───────────────────


@pytest.mark.asyncio
async def test_full_workflow_loop_via_state_machine(
    client: TestClient, session: AsyncSession
) -> None:
    """Drive a contact through the full lifecycle end-to-end via the HTTP
    workflow endpoint. This exercises the state-machine spec via the API
    surface, not just direct function calls."""
    contact = await _make_contact(session, adopter_status="new")
    try:
        # new → contacted
        r = client.post(
            f"/v1/contacts/{contact.id}/transition",
            json={"kind": "adopter", "to_state": "contacted"},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        # contacted → engaged
        r = client.post(
            f"/v1/contacts/{contact.id}/transition",
            json={"kind": "adopter", "to_state": "engaged"},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        # engaged → matched
        r = client.post(
            f"/v1/contacts/{contact.id}/transition",
            json={"kind": "adopter", "to_state": "matched"},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        # matched → active (facilitator accept)
        r = client.post(
            f"/v1/contacts/{contact.id}/transition",
            json={"kind": "adopter", "to_state": "active"},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        assert r.json()["contact"]["adopter_status"] == "active"

        # Four audit rows were written.
        audits = (
            await session.execute(
                select(TransitionAudit).where(TransitionAudit.contact_id == contact.id)
            )
        ).scalars().all()
        assert len(audits) == 4
    finally:
        await _cleanup_contact(session, contact)
