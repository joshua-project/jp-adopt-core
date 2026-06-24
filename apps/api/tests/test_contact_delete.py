"""U5: DELETE /v1/contacts/{id} hard-delete.

Covers the irreversible spam-delete path: a single transaction that removes
the contact and every child row, purges the no-FK orphans (identity_link,
migration_conflicts), records a deleted_contacts suppression row, and emits a
contact.deleted outbox event. Runs against the local Postgres (see conftest).
Rows are tagged origin='delete_test' for cleanup.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
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
    ContactAssignment,
    ContactProfile,
    DeletedContact,
    FacilitatingOrg,
    IdentityLink,
    Match,
    MatchAttempt,
    MigrationConflict,
    Outbox,
    TransitionAudit,
)

os.environ.setdefault("STRICT_AUTH", "false")
os.environ.setdefault("APP_ENV", "development")
get_settings.cache_clear()

AUTH = {"Authorization": "Bearer dev-local"}
EVENT_CONTACT_DELETED = "jp.adopt.v1.contact.deleted"


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _cleanup(session: AsyncSession, org_id: uuid.UUID, email: str) -> None:
    # transition_audit has no cascade; delete it first in case the test left
    # the contact in place. Then the contact (cascades children) and org.
    res = await session.execute(
        select(Contact.id).where(Contact.origin == "delete_test")
    )
    for (cid,) in res.all():
        await session.execute(
            delete(TransitionAudit).where(TransitionAudit.contact_id == cid)
        )
    await session.execute(delete(Contact).where(Contact.origin == "delete_test"))
    await session.execute(delete(FacilitatingOrg).where(FacilitatingOrg.id == org_id))
    await session.execute(
        delete(IdentityLink).where(IdentityLink.email_normalized == email)
    )
    await session.execute(
        delete(MigrationConflict).where(MigrationConflict.source_system == "dt")
        .where(MigrationConflict.table_name == "delete_test")
    )
    await session.execute(
        delete(DeletedContact).where(DeletedContact.email_normalized == email)
    )
    # DT-sourced deletes write a NULL-email suppression row keyed on source;
    # clean those too (the fixture contact is source_system='dt').
    await session.execute(
        delete(DeletedContact).where(DeletedContact.source_system == "dt")
    )
    await session.execute(
        delete(Outbox).where(Outbox.event_type == EVENT_CONTACT_DELETED)
    )
    await session.commit()


@pytest_asyncio.fixture
async def dt_contact(session: AsyncSession) -> AsyncIterator[Contact]:
    """A DT-sourced adopter wired up with every child + the no-FK orphans.

    profile + consent-less but: interest, match, assignment, activity,
    transition_audit, match_attempt, plus an identity_link (by email) and a
    migration_conflicts row (by source_system+source_id).
    """
    email = f"delete-{uuid.uuid4().hex[:10]}@example.com"
    org = FacilitatingOrg(
        id=uuid.uuid4(), name=f"DeleteTest Org {uuid.uuid4().hex[:6]}"
    )
    contact = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name="Delete Test Adopter",
        adopter_status="new",
        email_normalized=email,
        b2c_subject_id=f"subj-{uuid.uuid4().hex[:10]}",
        source_system="dt",
        source_id=f"dt-{uuid.uuid4().hex[:8]}",
        origin="delete_test",
    )
    session.add_all([org, contact])
    await session.flush()
    interest = AdopterInterest(
        id=uuid.uuid4(), contact_id=contact.id, people_id3=None
    )
    session.add(interest)
    await session.flush()
    session.add_all(
        [
            ContactProfile(id=uuid.uuid4(), contact_id=contact.id),
            ContactAssignment(contact_id=contact.id, user_subject_id="dev-local"),
            Match(
                id=uuid.uuid4(),
                adopter_interest_id=interest.id,
                facilitator_org_id=org.id,
                status="recommended",
            ),
            MatchAttempt(
                id=uuid.uuid4(),
                contact_id=contact.id,
                adopter_interest_id=interest.id,
                run_id=uuid.uuid4(),
                candidate_facilitator_id=org.id,
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
                body="seeded delete note",
                kind="note",
                occurred_at=datetime.now(UTC),
            ),
            IdentityLink(
                id=uuid.uuid4(),
                b2c_subject_id=contact.b2c_subject_id,
                email=email,
                email_normalized=email,
                idp_name="b2c",
            ),
            MigrationConflict(
                id=uuid.uuid4(),
                source_system="dt",
                source_id=contact.source_id,
                table_name="delete_test",
                conflict_type="local_modified",
            ),
        ]
    )
    await session.commit()
    yield contact
    await _cleanup(session, org.id, email)


async def _count(session: AsyncSession, model, *predicates) -> int:
    stmt = select(func.count()).select_from(model)
    for p in predicates:
        stmt = stmt.where(p)
    return int((await session.execute(stmt)).scalar_one())


@pytest.mark.asyncio
async def test_delete_removes_all_children_and_returns_204(
    client: TestClient, session: AsyncSession, dt_contact: Contact
):
    cid = dt_contact.id
    r = client.delete(f"/v1/contacts/{cid}", headers=AUTH)
    assert r.status_code == 204, r.text
    assert r.content == b""

    assert await _count(session, Contact, Contact.id == cid) == 0
    assert await _count(session, ContactProfile, ContactProfile.contact_id == cid) == 0
    assert (
        await _count(session, ContactAssignment, ContactAssignment.contact_id == cid)
        == 0
    )
    assert await _count(session, ActivityLog, ActivityLog.contact_id == cid) == 0
    assert (
        await _count(session, AdopterInterest, AdopterInterest.contact_id == cid) == 0
    )
    assert await _count(session, MatchAttempt, MatchAttempt.contact_id == cid) == 0
    # Match cascades through its adopter_interest (interest gone -> match gone).
    match_for_contact = (
        select(func.count())
        .select_from(Match)
        .join(AdopterInterest, AdopterInterest.id == Match.adopter_interest_id)
        .where(AdopterInterest.contact_id == cid)
    )
    assert int((await session.execute(match_for_contact)).scalar_one()) == 0


@pytest.mark.asyncio
async def test_delete_clears_transition_audit_no_fk_violation(
    client: TestClient, session: AsyncSession, dt_contact: Contact
):
    """Regression guard: transition_audit has no ON DELETE CASCADE, so the
    endpoint must delete it explicitly — otherwise the contact delete would
    FK-violate."""
    cid = dt_contact.id
    # Sanity: the fixture seeded a transition_audit row.
    before = await _count(session, TransitionAudit, TransitionAudit.contact_id == cid)
    assert before == 1
    r = client.delete(f"/v1/contacts/{cid}", headers=AUTH)
    assert r.status_code == 204, r.text
    after = await _count(session, TransitionAudit, TransitionAudit.contact_id == cid)
    assert after == 0


@pytest.mark.asyncio
async def test_delete_dt_contact_writes_suppression_by_source(
    client: TestClient, session: AsyncSession, dt_contact: Contact
):
    src_system, src_id = dt_contact.source_system, dt_contact.source_id
    r = client.delete(f"/v1/contacts/{dt_contact.id}", headers=AUTH)
    assert r.status_code == 204, r.text
    row = (
        await session.execute(
            select(DeletedContact).where(
                DeletedContact.source_system == src_system,
                DeletedContact.source_id == src_id,
            )
        )
    ).scalar_one()
    # DT-sourced: suppression is keyed on (source_system, source_id) only;
    # email_normalized is NULL so it can't skip other DT contacts that reuse
    # the email (MAJOR 2).
    assert row.email_normalized is None
    assert row.deleted_by == "dev-local"


@pytest.mark.asyncio
async def test_delete_forms_contact_writes_suppression_by_email(
    client: TestClient, session: AsyncSession
):
    """A forms contact has no source_id — the suppression row is keyed on
    email_normalized instead."""
    email = f"forms-{uuid.uuid4().hex[:10]}@example.com"
    contact = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name="Forms Delete Adopter",
        adopter_status="new",
        email_normalized=email,
        origin="delete_test",
    )
    session.add(contact)
    await session.commit()
    try:
        r = client.delete(f"/v1/contacts/{contact.id}", headers=AUTH)
        assert r.status_code == 204, r.text
        row = (
            await session.execute(
                select(DeletedContact).where(
                    DeletedContact.email_normalized == email
                )
            )
        ).scalar_one()
        assert row.source_system is None
        assert row.source_id is None
    finally:
        await session.execute(
            delete(DeletedContact).where(DeletedContact.email_normalized == email)
        )
        await session.execute(delete(Contact).where(Contact.id == contact.id))
        await session.execute(
            delete(Outbox).where(Outbox.event_type == EVENT_CONTACT_DELETED)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_delete_emits_outbox_event(
    client: TestClient, session: AsyncSession, dt_contact: Contact
):
    cid = dt_contact.id
    r = client.delete(f"/v1/contacts/{cid}", headers=AUTH)
    assert r.status_code == 204, r.text
    row = (
        await session.execute(
            select(Outbox)
            .where(Outbox.event_type == EVENT_CONTACT_DELETED)
            .order_by(Outbox.created_at.desc())
        )
    ).scalars().first()
    assert row is not None
    assert row.payload_json["contact_id"] == str(cid)
    assert row.payload_json["actor_subject_id"] == "dev-local"


@pytest.mark.asyncio
async def test_delete_purges_identity_and_conflict_orphans(
    client: TestClient, session: AsyncSession, dt_contact: Contact
):
    email = dt_contact.email_normalized
    src_id = dt_contact.source_id
    # Sanity: orphans exist before delete.
    assert (
        await _count(session, IdentityLink, IdentityLink.email_normalized == email) == 1
    )
    assert (
        await _count(
            session,
            MigrationConflict,
            MigrationConflict.source_system == "dt",
            MigrationConflict.source_id == src_id,
        )
        == 1
    )
    r = client.delete(f"/v1/contacts/{dt_contact.id}", headers=AUTH)
    assert r.status_code == 204, r.text
    assert (
        await _count(session, IdentityLink, IdentityLink.email_normalized == email) == 0
    )
    assert (
        await _count(
            session,
            MigrationConflict,
            MigrationConflict.source_system == "dt",
            MigrationConflict.source_id == src_id,
        )
        == 0
    )


@pytest.mark.asyncio
async def test_delete_spares_other_users_link_sharing_email(
    client: TestClient, session: AsyncSession, dt_contact: Contact
):
    """MAJOR 1 regression: IdentityLink.email_normalized is NOT globally
    unique — B2C links can share an email across distinct b2c_subject_ids.
    A second, non-magic-link IdentityLink with the same email but a DIFFERENT
    b2c_subject_id belongs to a different user and MUST survive the delete.
    Only the deleted contact's own subject-matched link is purged."""
    email = dt_contact.email_normalized
    other_subject = f"subj-other-{uuid.uuid4().hex[:10]}"
    survivor = IdentityLink(
        id=uuid.uuid4(),
        b2c_subject_id=other_subject,
        email=email,
        email_normalized=email,
        idp_name="b2c",  # NOT magic_link → must not be purged by email
    )
    session.add(survivor)
    await session.commit()
    survivor_id = survivor.id
    try:
        r = client.delete(f"/v1/contacts/{dt_contact.id}", headers=AUTH)
        assert r.status_code == 204, r.text
        # The contact's own (subject-matched) link is gone …
        assert (
            await _count(
                session,
                IdentityLink,
                IdentityLink.b2c_subject_id == dt_contact.b2c_subject_id,
            )
            == 0
        )
        # … but the other user's email-sharing link survives.
        assert (
            await _count(session, IdentityLink, IdentityLink.id == survivor_id) == 1
        )
    finally:
        await session.execute(
            delete(IdentityLink).where(IdentityLink.id == survivor_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_delete_purges_magic_link_by_email(
    client: TestClient, session: AsyncSession, dt_contact: Contact
):
    """MAJOR 1: magic_link is the only email-unique IdentityLink class, so a
    magic-link row matched by email (even with no/other subject) IS purged."""
    email = dt_contact.email_normalized
    magic = IdentityLink(
        id=uuid.uuid4(),
        b2c_subject_id=None,
        email=email,
        email_normalized=email,
        idp_name="magic_link",
    )
    session.add(magic)
    await session.commit()
    magic_id = magic.id
    try:
        r = client.delete(f"/v1/contacts/{dt_contact.id}", headers=AUTH)
        assert r.status_code == 204, r.text
        assert await _count(session, IdentityLink, IdentityLink.id == magic_id) == 0
    finally:
        await session.execute(
            delete(IdentityLink).where(IdentityLink.id == magic_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_delete_dt_contact_suppression_omits_email(
    client: TestClient, session: AsyncSession, dt_contact: Contact
):
    """MAJOR 2: a DT-sourced delete suppresses strictly by (source_system,
    source_id). email_normalized must be NULL on the suppression row so the
    ETL does not skip OTHER DT contacts that reuse the same email."""
    src_system, src_id = dt_contact.source_system, dt_contact.source_id
    r = client.delete(f"/v1/contacts/{dt_contact.id}", headers=AUTH)
    assert r.status_code == 204, r.text
    row = (
        await session.execute(
            select(DeletedContact).where(
                DeletedContact.source_system == src_system,
                DeletedContact.source_id == src_id,
            )
        )
    ).scalar_one()
    assert row.email_normalized is None
    # Cleanup is keyed on email in the fixture; delete by source here too.
    await session.execute(
        delete(DeletedContact).where(
            DeletedContact.source_system == src_system,
            DeletedContact.source_id == src_id,
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_delete_with_match_attempt_rows_no_fk_violation(
    client: TestClient, session: AsyncSession, dt_contact: Contact
):
    """Regression for the latent match_attempt FK: a contact with
    match_attempt rows hard-deletes cleanly (the FK cascades), with no FK
    violation. The dt_contact fixture seeds one match_attempt row."""
    cid = dt_contact.id
    before = await _count(session, MatchAttempt, MatchAttempt.contact_id == cid)
    assert before == 1
    r = client.delete(f"/v1/contacts/{cid}", headers=AUTH)
    assert r.status_code == 204, r.text
    assert await _count(session, MatchAttempt, MatchAttempt.contact_id == cid) == 0
    assert await _count(session, Contact, Contact.id == cid) == 0


@pytest.mark.asyncio
async def test_delete_missing_id_returns_404(client: TestClient):
    r = client.delete(f"/v1/contacts/{uuid.uuid4()}", headers=AUTH)
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_delete_non_privileged_role_returns_403(
    client: TestClient,
    session: AsyncSession,
    dt_contact: Contact,
    monkeypatch: pytest.MonkeyPatch,
):
    from jp_adopt_api import deps as deps_module

    async def _fake_roles(db: object, user_sub: str) -> frozenset[str]:
        return frozenset({"facilitator"})

    monkeypatch.setattr(deps_module, "load_user_roles", _fake_roles)
    r = client.delete(f"/v1/contacts/{dt_contact.id}", headers=AUTH)
    assert r.status_code == 403, r.text
    # Contact must survive the rejected request.
    assert await _count(session, Contact, Contact.id == dt_contact.id) == 1
