"""U7 matches API tests.

Covers the test scenarios called out in the plan:
  * happy path — GET /v1/matches/queue returns grouped recommendations
    with score breakdown for staff;
  * happy path — POST /decide accept transitions Contact + Match;
  * happy path — POST /decide send_back with reason transitions to sent_back
    and the next match_or_route run excludes the prior facilitator;
  * edge case — already-decided Match: 409 on different decision, 200 on same;
  * edge case — facilitator org isolation: 403 on cross-org access;
  * edge case — alternates promotion via route_elsewhere;
  * #40 — POST /v1/matches/run/{contact_id} triggers match_or_route over HTTP.
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
    AdopterInterest,
    Contact,
    FacilitatingOrg,
    FacilitatorFpgCoverage,
    FacilitatorOrgMembership,
    Fpg,
    Match,
    MatchAttempt,
    Outbox,
    Role,
    TransitionAudit,
    UserRole,
)

# Force STRICT_AUTH off so dev-local bearer works in tests; mirror the pattern
# used by test_intake.py.
os.environ.setdefault("STRICT_AUTH", "false")
os.environ.setdefault("APP_ENV", "development")
get_settings.cache_clear()


# ─── helpers ────────────────────────────────────────────────────────────────


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
    adopter_status: str = "new",
    display_name: str = "Test Adopter",
    country: str = "US",
    languages: list[str] | None = None,
) -> Contact:
    contact = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name=display_name,
        adopter_status=adopter_status,
        email_normalized=f"matches-{uuid.uuid4().hex[:10]}@example.com",
        country_code=country,
        language_codes=languages or ["en"],
    )
    session.add(contact)
    await session.flush()
    await session.commit()
    return contact


async def _ensure_fpg(session: AsyncSession, people_id3: str) -> None:
    existing_fpg = await session.get(Fpg, people_id3)
    if existing_fpg is None:
        session.add(
            Fpg(people_id3=people_id3, name=f"Test FPG {people_id3}", country_code="US", frontier=True)
        )
        await session.flush()
        await session.commit()


async def _make_interest(
    session: AsyncSession, contact: Contact, people_id3: str | None
) -> AdopterInterest:
    if people_id3 is not None:
        await _ensure_fpg(session, people_id3)
    interest = AdopterInterest(
        id=uuid.uuid4(), contact_id=contact.id, people_id3=people_id3
    )
    session.add(interest)
    await session.flush()
    await session.commit()
    return interest


async def _make_org_with_coverage(
    session: AsyncSession,
    *,
    people_id3: str,
    capacity_total: int = 5,
    capacity_committed: int = 0,
) -> FacilitatingOrg:
    await _ensure_fpg(session, people_id3)
    org = FacilitatingOrg(
        id=uuid.uuid4(),
        name=f"Org {people_id3} {uuid.uuid4().hex[:6]}",
        country_code="US",
        language_codes=["en"],
        capacity_total=capacity_total,
        capacity_committed=capacity_committed,
        active=True,
        is_triage_org=False,
    )
    session.add(org)
    session.add(FacilitatorFpgCoverage(facilitator_org_id=org.id, people_id3=people_id3))
    await session.flush()
    await session.commit()
    return org


async def _seed_role(session: AsyncSession, user_sub: str, role_name: str) -> None:
    role = (
        await session.execute(select(Role).where(Role.name == role_name))
    ).scalar_one_or_none()
    if role is None:
        raise RuntimeError(
            f"Role {role_name!r} not seeded; migration 0003 should have inserted it"
        )
    # Idempotent insert: ON CONFLICT DO NOTHING via existence check (the table
    # has a composite PK so a second insert raises).
    existing = (
        await session.execute(
            select(UserRole).where(
                UserRole.user_subject_id == user_sub,
                UserRole.role_id == role.id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(UserRole(user_subject_id=user_sub, role_id=role.id))
        await session.commit()


async def _grant_org_membership(
    session: AsyncSession, *, user_sub: str, org_id: uuid.UUID
) -> None:
    existing = (
        await session.execute(
            select(FacilitatorOrgMembership).where(
                FacilitatorOrgMembership.user_subject_id == user_sub,
                FacilitatorOrgMembership.facilitator_org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            FacilitatorOrgMembership(
                user_subject_id=user_sub,
                facilitator_org_id=org_id,
            )
        )
        await session.commit()


async def _cleanup_contact_chain(
    session: AsyncSession, contact: Contact
) -> None:
    interest_ids = (
        await session.execute(
            select(AdopterInterest.id).where(AdopterInterest.contact_id == contact.id)
        )
    ).scalars().all()
    if interest_ids:
        await session.execute(
            delete(Match).where(Match.adopter_interest_id.in_(interest_ids))
        )
        await session.execute(
            delete(MatchAttempt).where(
                MatchAttempt.adopter_interest_id.in_(interest_ids)
            )
        )
    # transition_audit lacks an ON DELETE CASCADE, so wipe rows that
    # reference this contact before deleting it.
    await session.execute(
        delete(TransitionAudit).where(TransitionAudit.contact_id == contact.id)
    )
    await session.execute(delete(Contact).where(Contact.id == contact.id))
    await session.commit()


async def _cleanup_org(session: AsyncSession, org_id: uuid.UUID) -> None:
    # match_attempt has no ON DELETE on candidate_facilitator_id, so wipe
    # those rows first to clear the FK before deleting the org.
    await session.execute(
        delete(MatchAttempt).where(MatchAttempt.candidate_facilitator_id == org_id)
    )
    await session.execute(
        delete(Match).where(Match.facilitator_org_id == org_id)
    )
    await session.execute(
        delete(FacilitatorFpgCoverage).where(
            FacilitatorFpgCoverage.facilitator_org_id == org_id
        )
    )
    await session.execute(
        delete(FacilitatorOrgMembership).where(
            FacilitatorOrgMembership.facilitator_org_id == org_id
        )
    )
    await session.execute(delete(FacilitatingOrg).where(FacilitatingOrg.id == org_id))
    await session.commit()


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_test_orgs_at_session_end(
    session: AsyncSession,
) -> AsyncIterator[None]:
    """Belt-and-suspenders cleanup: between tests, drop any TST-prefixed orgs
    + FPGs that an earlier crash may have orphaned. Keeps the seeded
    Triage/Example/Frontier rows untouched so test_seed_data_present still
    passes regardless of run order.
    """
    yield
    seed_org_ids = {
        uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1"),
        uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2"),
        uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb3"),
    }
    orphan_org_ids = (
        await session.execute(
            select(FacilitatingOrg.id).where(
                FacilitatingOrg.name.like("Org TST%")
            )
        )
    ).scalars().all()
    for oid in orphan_org_ids:
        if oid not in seed_org_ids:
            await _cleanup_org(session, oid)
    await session.execute(
        delete(Fpg).where(Fpg.people_id3.like("TST%"))
    )
    await session.commit()


async def _latest_outbox_events(
    session: AsyncSession, *, event_type_prefix: str, limit: int = 5
) -> list[dict[str, object]]:
    rows = (
        await session.execute(
            select(Outbox)
            .where(Outbox.event_type.like(f"{event_type_prefix}%"))
            .order_by(Outbox.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [r.payload_json for r in rows]


# ─── queue endpoint ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_queue_returns_open_matches_for_staff(
    client: TestClient, session: AsyncSession
) -> None:
    """dev-local has staff_admin (via _DEV_LOCAL_ROLES) so it sees every open
    Match. Run /run/{contact_id} to populate the queue, then GET /queue and
    assert the seeded match is present with its score breakdown."""
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session)
    await _make_interest(session, contact, people_id3)
    org = await _make_org_with_coverage(session, people_id3=people_id3)

    try:
        r = client.post(
            f"/v1/matches/run/{contact.id}", json={}, headers=_auth_headers()
        )
        assert r.status_code == 200, r.text
        run_resp = r.json()
        assert run_resp["total_recommended"] == 1

        r = client.get("/v1/matches/queue", headers=_auth_headers())
        assert r.status_code == 200, r.text
        body = r.json()
        ids = {item["contact_id"] for item in body["items"]}
        assert str(contact.id) in ids
        ours = next(i for i in body["items"] if i["contact_id"] == str(contact.id))
        assert ours["status"] == "recommended"
        assert ours["people_id3"] == people_id3
        # Score breakdown is embedded on each candidate.
        assert ours["candidates"], "Expected ranked alternates"
        first = ours["candidates"][0]
        assert first["score"] is not None
        assert set(first["score_breakdown"].keys()) == {
            "capacity_headroom",
            "geography",
            "language",
            "fpg_affinity",
            "theological",
        }
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, org.id)


@pytest.mark.asyncio
async def test_queue_unauthenticated_returns_401(client: TestClient) -> None:
    r = client.get("/v1/matches/queue")
    assert r.status_code == 401


# ─── decide endpoint ────────────────────────────────────────────────────────


async def _seed_recommended_match(
    session: AsyncSession, contact: Contact, org: FacilitatingOrg, people_id3: str
) -> Match:
    interest = await _make_interest(session, contact, people_id3)
    m = Match(
        id=uuid.uuid4(),
        adopter_interest_id=interest.id,
        facilitator_org_id=org.id,
        status="recommended",
    )
    session.add(m)
    # Add a single ranked MatchAttempt so route_elsewhere has somewhere to go
    # (irrelevant for accept / send_back paths but useful for shared fixtures).
    session.add(
        MatchAttempt(
            id=uuid.uuid4(),
            contact_id=contact.id,
            adopter_interest_id=interest.id,
            run_id=uuid.uuid4(),
            candidate_facilitator_id=org.id,
            score=None,
            score_breakdown=None,
            filter_results=None,
            rank=1,
        )
    )
    await session.commit()
    return m


@pytest.mark.asyncio
async def test_decide_accept_transitions_contact_to_matched(
    client: TestClient, session: AsyncSession
) -> None:
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session, adopter_status="new")
    org = await _make_org_with_coverage(session, people_id3=people_id3, capacity_committed=0)
    m = await _seed_recommended_match(session, contact, org, people_id3)
    initial_committed = org.capacity_committed
    try:
        r = client.post(
            f"/v1/matches/{m.id}/decide",
            json={"decision": "accept"},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["contact_adopter_status"] == "matched"
        assert body["match"]["status"] == "accepted"
        # Capacity reservation was bumped on the first accept.
        await session.refresh(org)
        assert org.capacity_committed == initial_committed + 1
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, org.id)


@pytest.mark.asyncio
async def test_decide_send_back_excludes_facilitator_on_rematch(
    client: TestClient, session: AsyncSession
) -> None:
    """After Amy sends back a recommendation, a fresh match_or_route run for
    the same contact must not re-recommend the same facilitator."""
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session, adopter_status="matched")
    org_a = await _make_org_with_coverage(session, people_id3=people_id3)
    org_b = await _make_org_with_coverage(session, people_id3=people_id3)
    interest = await _make_interest(session, contact, people_id3)
    m = Match(
        id=uuid.uuid4(),
        adopter_interest_id=interest.id,
        facilitator_org_id=org_a.id,
        status="recommended",
    )
    session.add(m)
    await session.commit()

    try:
        r = client.post(
            f"/v1/matches/{m.id}/decide",
            json={"decision": "send_back", "reason_code": "capacity_full"},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["match"]["status"] == "sent_back"
        assert body["contact_adopter_status"] == "sent_back"

        # Trigger a fresh run; verify the new recommendation is org_b, NOT org_a.
        # The current adopter_status is sent_back — first transition to matched
        # via SENT_BACK→MATCHED then run /run (with force, since the previous
        # accept reserved capacity).
        # Actually the run endpoint doesn't change the contact status; it just
        # produces ranking. So we can call it directly.
        # The send-back marked the prior Match.status='sent_back'; the matching
        # algo's _derive_exclusion_list picks up exclusions from that row.
        r = client.post(
            f"/v1/matches/run/{contact.id}",
            json={"force": True},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text

        # Read the open recommendation; it should NOT point at org_a.
        open_matches = (
            await session.execute(
                select(Match)
                .where(Match.adopter_interest_id == interest.id)
                .where(Match.status == "recommended")
            )
        ).scalars().all()
        recommended_orgs = {m.facilitator_org_id for m in open_matches}
        assert org_a.id not in recommended_orgs, (
            f"Expected exclusion of {org_a.id}, got {recommended_orgs}"
        )
        assert org_b.id in recommended_orgs, (
            f"Expected re-match to {org_b.id}, got {recommended_orgs}"
        )
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, org_a.id)
        await _cleanup_org(session, org_b.id)


@pytest.mark.asyncio
async def test_decide_send_back_without_reason_succeeds(
    client: TestClient, session: AsyncSession
) -> None:
    """F2: the decline reason is optional — a send-back with no reason_code
    now succeeds (200) and records a null reason."""
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session, adopter_status="matched")
    org = await _make_org_with_coverage(session, people_id3=people_id3)
    m = await _seed_recommended_match(session, contact, org, people_id3)
    try:
        r = client.post(
            f"/v1/matches/{m.id}/decide",
            json={"decision": "send_back"},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        assert r.json()["match"]["status"] == "sent_back"
        await session.refresh(m)
        assert m.status == "sent_back"
        assert m.decision_reason_code is None
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, org.id)


@pytest.mark.asyncio
async def test_decide_already_decided_same_decision_is_idempotent(
    client: TestClient, session: AsyncSession
) -> None:
    """F20: a retry of ``accept`` against a match that's already at
    ``accepted`` (manager accept) OR ``active`` (manager → facilitator
    accept) must be a deterministic 200. The previous test admitted both
    200 and 409 because the idempotency check only matched ``accepted``."""
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session, adopter_status="new")
    org = await _make_org_with_coverage(session, people_id3=people_id3)
    m = await _seed_recommended_match(session, contact, org, people_id3)
    try:
        r = client.post(
            f"/v1/matches/{m.id}/decide",
            json={"decision": "accept"},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        # First accept landed: match=accepted, contact=matched.
        # Second accept (same body) — the contact is now `matched`, so the
        # endpoint legitimately performs the matched→active transition and
        # returns 200 (match=active). This is the documented facilitator
        # accept flow.
        r2 = client.post(
            f"/v1/matches/{m.id}/decide",
            json={"decision": "accept"},
            headers=_auth_headers(),
        )
        assert r2.status_code == 200, r2.text
        # Third accept — match is now `active`, contact is `active`. The
        # idempotency check should catch this and return the current state.
        r3 = client.post(
            f"/v1/matches/{m.id}/decide",
            json={"decision": "accept"},
            headers=_auth_headers(),
        )
        assert r3.status_code == 200, r3.text
        assert r3.json()["match"]["status"] == "active"
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, org.id)


@pytest.mark.asyncio
async def test_decide_terminal_state_returns_409(
    client: TestClient, session: AsyncSession
) -> None:
    """A match that's reached a terminal state (declined via route_elsewhere)
    is no longer in the queue and can't be acted on again."""
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session, adopter_status="new")
    org_a = await _make_org_with_coverage(session, people_id3=people_id3)
    org_b = await _make_org_with_coverage(session, people_id3=people_id3)
    interest = await _make_interest(session, contact, people_id3)
    m = Match(
        id=uuid.uuid4(),
        adopter_interest_id=interest.id,
        facilitator_org_id=org_a.id,
        status="recommended",
    )
    session.add(m)
    # Two ranked alternates so route_elsewhere has an org_b to promote.
    session.add_all(
        [
            MatchAttempt(
                id=uuid.uuid4(),
                contact_id=contact.id,
                adopter_interest_id=interest.id,
                run_id=uuid.uuid4(),
                candidate_facilitator_id=org_a.id,
                score=None,
                score_breakdown=None,
                filter_results=None,
                rank=1,
            ),
            MatchAttempt(
                id=uuid.uuid4(),
                contact_id=contact.id,
                adopter_interest_id=interest.id,
                run_id=uuid.uuid4(),
                candidate_facilitator_id=org_b.id,
                score=None,
                score_breakdown=None,
                filter_results=None,
                rank=2,
            ),
        ]
    )
    await session.commit()

    try:
        # route_elsewhere drops m to status='declined' (terminal).
        r = client.post(
            f"/v1/matches/{m.id}/decide",
            json={"decision": "route_elsewhere"},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        assert r.json()["match"]["status"] == "declined"

        # Now a follow-up accept on the declined match must be 409.
        r2 = client.post(
            f"/v1/matches/{m.id}/decide",
            json={"decision": "accept"},
            headers=_auth_headers(),
        )
        assert r2.status_code == 409, r2.text
        assert r2.json()["detail"]["code"] == "match_already_decided"
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, org_a.id)
        await _cleanup_org(session, org_b.id)


@pytest.mark.asyncio
async def test_decide_accept_emits_outbox_event(
    client: TestClient, session: AsyncSession
) -> None:
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session, adopter_status="new")
    org = await _make_org_with_coverage(session, people_id3=people_id3)
    m = await _seed_recommended_match(session, contact, org, people_id3)
    try:
        r = client.post(
            f"/v1/matches/{m.id}/decide",
            json={"decision": "accept"},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        events = await _latest_outbox_events(
            session, event_type_prefix="jp.adopt.v1.match.accepted"
        )
        assert any(
            e.get("match_id") == str(m.id) for e in events
        ), f"Expected accept event for match {m.id} in {events}"
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, org.id)


# ─── run endpoint (#40) ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_refuses_when_open_match_exists_without_force(
    client: TestClient, session: AsyncSession
) -> None:
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session)
    org = await _make_org_with_coverage(session, people_id3=people_id3)
    await _seed_recommended_match(session, contact, org, people_id3)
    try:
        r = client.post(
            f"/v1/matches/run/{contact.id}",
            json={},
            headers=_auth_headers(),
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "open_match_exists"
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, org.id)


@pytest.mark.asyncio
async def test_run_with_force_overwrites_existing_open_match(
    client: TestClient, session: AsyncSession
) -> None:
    """With force=true, /run is allowed to re-rank candidates even when the
    contact already has an open match. The matcher hits the uq_match_open_per_interest
    conflict guard; the outcome is still well-defined (savepoint refetch
    returns the existing winner, no exception leaks)."""
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session)
    org = await _make_org_with_coverage(session, people_id3=people_id3)
    await _seed_recommended_match(session, contact, org, people_id3)
    try:
        r = client.post(
            f"/v1/matches/run/{contact.id}",
            json={"force": True},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, org.id)


@pytest.mark.asyncio
async def test_run_404_on_missing_contact(client: TestClient) -> None:
    missing = uuid.uuid4()
    r = client.post(
        f"/v1/matches/run/{missing}", json={}, headers=_auth_headers()
    )
    assert r.status_code == 404


# ─── F25 / F26: route_elsewhere positive assertions + outbox events ──────


@pytest.mark.asyncio
async def test_decide_route_elsewhere_creates_new_recommended(
    client: TestClient, session: AsyncSession
) -> None:
    """F25: assert the post-conditions of a successful route_elsewhere:
    the original Match is declined, a new ``recommended`` Match is created
    against the chosen alternate's facilitator, and the new Match is
    properly visible via the queue."""
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session, adopter_status="new")
    org_a = await _make_org_with_coverage(session, people_id3=people_id3)
    org_b = await _make_org_with_coverage(session, people_id3=people_id3)
    interest = await _make_interest(session, contact, people_id3)
    m = Match(
        id=uuid.uuid4(),
        adopter_interest_id=interest.id,
        facilitator_org_id=org_a.id,
        status="recommended",
    )
    session.add(m)
    session.add_all(
        [
            MatchAttempt(
                id=uuid.uuid4(),
                contact_id=contact.id,
                adopter_interest_id=interest.id,
                run_id=uuid.uuid4(),
                candidate_facilitator_id=org_a.id,
                rank=1,
            ),
            MatchAttempt(
                id=uuid.uuid4(),
                contact_id=contact.id,
                adopter_interest_id=interest.id,
                run_id=uuid.uuid4(),
                candidate_facilitator_id=org_b.id,
                rank=2,
            ),
        ]
    )
    await session.commit()

    try:
        r = client.post(
            f"/v1/matches/{m.id}/decide",
            json={"decision": "route_elsewhere"},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["match"]["status"] == "declined"

        # The new Match must exist, point at org_b, and be `recommended`.
        new_open = (
            await session.execute(
                select(Match)
                .where(Match.adopter_interest_id == interest.id)
                .where(Match.status == "recommended")
            )
        ).scalars().all()
        assert len(new_open) == 1, (
            f"Expected exactly one new recommended Match, got {len(new_open)}"
        )
        assert new_open[0].facilitator_org_id == org_b.id
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, org_a.id)
        await _cleanup_org(session, org_b.id)


@pytest.mark.asyncio
async def test_decide_route_elsewhere_no_alternates_returns_409(
    client: TestClient, session: AsyncSession
) -> None:
    """F25: with no ranked alternate available, route_elsewhere must 409
    with ``no_alternates`` rather than 500 or 200."""
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session, adopter_status="new")
    org_a = await _make_org_with_coverage(session, people_id3=people_id3)
    interest = await _make_interest(session, contact, people_id3)
    m = Match(
        id=uuid.uuid4(),
        adopter_interest_id=interest.id,
        facilitator_org_id=org_a.id,
        status="recommended",
    )
    session.add(m)
    # Only the primary attempt, no second-ranked alternate.
    session.add(
        MatchAttempt(
            id=uuid.uuid4(),
            contact_id=contact.id,
            adopter_interest_id=interest.id,
            run_id=uuid.uuid4(),
            candidate_facilitator_id=org_a.id,
            rank=1,
        )
    )
    await session.commit()

    try:
        r = client.post(
            f"/v1/matches/{m.id}/decide",
            json={"decision": "route_elsewhere"},
            headers=_auth_headers(),
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "no_alternates"
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, org_a.id)


@pytest.mark.asyncio
async def test_decide_route_elsewhere_invalid_next_attempt_id_returns_400(
    client: TestClient, session: AsyncSession
) -> None:
    """F25: an unknown ``next_attempt_id`` must surface as 400
    ``alternate_not_found``, not silently fall through to the highest-ranked."""
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session, adopter_status="new")
    org_a = await _make_org_with_coverage(session, people_id3=people_id3)
    org_b = await _make_org_with_coverage(session, people_id3=people_id3)
    interest = await _make_interest(session, contact, people_id3)
    m = Match(
        id=uuid.uuid4(),
        adopter_interest_id=interest.id,
        facilitator_org_id=org_a.id,
        status="recommended",
    )
    session.add(m)
    session.add(
        MatchAttempt(
            id=uuid.uuid4(),
            contact_id=contact.id,
            adopter_interest_id=interest.id,
            run_id=uuid.uuid4(),
            candidate_facilitator_id=org_b.id,
            rank=2,
        )
    )
    await session.commit()

    try:
        r = client.post(
            f"/v1/matches/{m.id}/decide",
            json={
                "decision": "route_elsewhere",
                "next_attempt_id": str(uuid.uuid4()),
            },
            headers=_auth_headers(),
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["code"] == "alternate_not_found"
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, org_a.id)
        await _cleanup_org(session, org_b.id)


@pytest.mark.asyncio
async def test_decide_send_back_emits_outbox_event(
    client: TestClient, session: AsyncSession
) -> None:
    """F26: send_back must emit ``jp.adopt.v1.match.sent_back`` (the
    state-machine's event_type for matched → sent_back)."""
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session, adopter_status="matched")
    org = await _make_org_with_coverage(session, people_id3=people_id3)
    m = await _seed_recommended_match(session, contact, org, people_id3)
    try:
        r = client.post(
            f"/v1/matches/{m.id}/decide",
            json={"decision": "send_back", "reason_code": "capacity_full"},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        events = await _latest_outbox_events(
            session, event_type_prefix="jp.adopt.v1.match.sent_back"
        )
        assert any(
            e.get("contact_id") == str(contact.id) for e in events
        ), f"Expected sent_back event for contact {contact.id} in {events}"
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, org.id)


@pytest.mark.asyncio
async def test_decide_route_elsewhere_emits_outbox_event(
    client: TestClient, session: AsyncSession
) -> None:
    """F26: route_elsewhere must emit ``jp.adopt.v1.match.routed_elsewhere``
    with both from/to facilitator_org_id populated."""
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session, adopter_status="new")
    org_a = await _make_org_with_coverage(session, people_id3=people_id3)
    org_b = await _make_org_with_coverage(session, people_id3=people_id3)
    interest = await _make_interest(session, contact, people_id3)
    m = Match(
        id=uuid.uuid4(),
        adopter_interest_id=interest.id,
        facilitator_org_id=org_a.id,
        status="recommended",
    )
    session.add(m)
    session.add(
        MatchAttempt(
            id=uuid.uuid4(),
            contact_id=contact.id,
            adopter_interest_id=interest.id,
            run_id=uuid.uuid4(),
            candidate_facilitator_id=org_b.id,
            rank=2,
        )
    )
    await session.commit()

    try:
        r = client.post(
            f"/v1/matches/{m.id}/decide",
            json={"decision": "route_elsewhere"},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        events = await _latest_outbox_events(
            session, event_type_prefix="jp.adopt.v1.match.routed_elsewhere"
        )
        match_event = next(
            (e for e in events if e.get("match_id") == str(m.id)),
            None,
        )
        assert match_event is not None, (
            f"Expected routed_elsewhere event for match {m.id} in {events}"
        )
        assert match_event["from_facilitator_org_id"] == str(org_a.id)
        assert match_event["to_facilitator_org_id"] == str(org_b.id)
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, org_a.id)
        await _cleanup_org(session, org_b.id)


# ─── F12: facilitator org-isolation 403 ──────────────────────────────────


@pytest.mark.asyncio
async def test_decide_facilitator_cross_org_returns_403(
    client: TestClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F12: a facilitator with org-A membership must NOT be able to decide
    a Match in org-B. We override ``load_user_roles`` for the test so the
    dev-local bearer presents as a pure facilitator (no staff_admin shortcut)
    and is granted membership only in org_b — so the match in org_a is
    inaccessible."""
    from jp_adopt_api import deps as deps_module

    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session, adopter_status="new")
    org_a = await _make_org_with_coverage(session, people_id3=people_id3)
    org_b = await _make_org_with_coverage(session, people_id3=people_id3)
    m = await _seed_recommended_match(session, contact, org_a, people_id3)
    # Grant the dev-local subject membership only in org_b — explicitly NOT
    # the org that owns the Match under test.
    await _grant_org_membership(session, user_sub="dev-local", org_id=org_b.id)

    async def _fake_roles(db: object, user_sub: str) -> frozenset[str]:
        return frozenset({"facilitator"})

    monkeypatch.setattr(deps_module, "load_user_roles", _fake_roles)
    try:
        r = client.post(
            f"/v1/matches/{m.id}/decide",
            json={"decision": "accept"},
            headers=_auth_headers(),
        )
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "org_not_member"
    finally:
        # Clean up the membership row first to avoid an orphan after _cleanup_org.
        await session.execute(
            delete(FacilitatorOrgMembership).where(
                FacilitatorOrgMembership.user_subject_id == "dev-local"
            )
        )
        await session.commit()
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, org_a.id)
        await _cleanup_org(session, org_b.id)


# ─── F24: next_attempt_id only valid with route_elsewhere ────────────────


@pytest.mark.asyncio
async def test_decide_accept_with_next_attempt_id_returns_422(
    client: TestClient, session: AsyncSession
) -> None:
    """F24: sending ``next_attempt_id`` with a non-``route_elsewhere`` verb
    is silently dropped today; surface it as a 422 from the schema layer."""
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session, adopter_status="new")
    org = await _make_org_with_coverage(session, people_id3=people_id3)
    m = await _seed_recommended_match(session, contact, org, people_id3)
    try:
        r = client.post(
            f"/v1/matches/{m.id}/decide",
            json={
                "decision": "accept",
                "next_attempt_id": str(uuid.uuid4()),
            },
            headers=_auth_headers(),
        )
        assert r.status_code == 422, r.text
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, org.id)


# ─── F1 (#52): assignable-orgs picker ────────────────────────────────────


@pytest.mark.asyncio
async def test_assignable_orgs_annotates_eligibility(
    client: TestClient, session: AsyncSession
) -> None:
    """Each active non-triage org is tagged covers_fpg / has_capacity /
    warning for the match's interest; the current match org is excluded."""
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    other = f"OTH{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session)
    current = await _make_org_with_coverage(session, people_id3=people_id3)
    m = await _seed_recommended_match(session, contact, current, people_id3)
    covering = await _make_org_with_coverage(
        session, people_id3=people_id3, capacity_total=5, capacity_committed=1
    )
    at_cap = await _make_org_with_coverage(
        session, people_id3=people_id3, capacity_total=2, capacity_committed=2
    )
    non_cov = await _make_org_with_coverage(session, people_id3=other)
    try:
        r = client.get(
            f"/v1/matches/{m.id}/assignable-orgs", headers=_auth_headers()
        )
        assert r.status_code == 200, r.text
        items = {i["facilitator_org_id"]: i for i in r.json()["items"]}
        assert str(current.id) not in items  # current org excluded
        assert items[str(covering.id)]["covers_fpg"] is True
        assert items[str(covering.id)]["has_capacity"] is True
        assert items[str(covering.id)]["warning"] is None
        assert items[str(at_cap.id)]["covers_fpg"] is True
        assert items[str(at_cap.id)]["has_capacity"] is False
        assert items[str(at_cap.id)]["warning"] == "at_capacity"
        assert items[str(non_cov.id)]["covers_fpg"] is False
        assert items[str(non_cov.id)]["warning"] == "no_coverage"
    finally:
        await _cleanup_contact_chain(session, contact)
        for o in (current, covering, at_cap, non_cov):
            await _cleanup_org(session, o.id)


@pytest.mark.asyncio
async def test_assignable_orgs_excludes_triage_and_inactive(
    client: TestClient, session: AsyncSession
) -> None:
    """Triage orgs (seeded bbb1) and inactive orgs never appear as
    assignable candidates."""
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session)
    current = await _make_org_with_coverage(session, people_id3=people_id3)
    m = await _seed_recommended_match(session, contact, current, people_id3)
    await _ensure_fpg(session, people_id3)
    inactive = FacilitatingOrg(
        id=uuid.uuid4(),
        name=f"Inactive {uuid.uuid4().hex[:6]}",
        country_code="US",
        language_codes=["en"],
        capacity_total=5,
        capacity_committed=0,
        active=False,
        is_triage_org=False,
    )
    session.add(inactive)
    session.add(
        FacilitatorFpgCoverage(facilitator_org_id=inactive.id, people_id3=people_id3)
    )
    await session.commit()
    try:
        r = client.get(
            f"/v1/matches/{m.id}/assignable-orgs", headers=_auth_headers()
        )
        assert r.status_code == 200, r.text
        ids = {i["facilitator_org_id"] for i in r.json()["items"]}
        assert str(inactive.id) not in ids
        assert "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1" not in ids  # seeded triage
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, current.id)
        await _cleanup_org(session, inactive.id)


@pytest.mark.asyncio
async def test_assignable_orgs_unknown_match_returns_404(
    client: TestClient,
) -> None:
    r = client.get(
        f"/v1/matches/{uuid.uuid4()}/assignable-orgs", headers=_auth_headers()
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_assignable_orgs_non_staff_returns_403(
    client: TestClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Override assignment is a manager capability — a pure facilitator is
    refused (403) even on a match they could otherwise view."""
    from jp_adopt_api import deps as deps_module

    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session)
    org = await _make_org_with_coverage(session, people_id3=people_id3)
    m = await _seed_recommended_match(session, contact, org, people_id3)

    async def _fake_roles(db: object, user_sub: str) -> frozenset[str]:
        return frozenset({"facilitator"})

    monkeypatch.setattr(deps_module, "load_user_roles", _fake_roles)
    try:
        r = client.get(
            f"/v1/matches/{m.id}/assignable-orgs", headers=_auth_headers()
        )
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "role_required"
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, org.id)


# ─── F1 (#52): route_elsewhere staff override ────────────────────────────


async def _open_recommended_match(
    session: AsyncSession, interest_id: uuid.UUID
) -> Match:
    # Fresh query — the override match was created by the app's session, so it
    # is not in this session's identity map and loads with committed state.
    return (
        await session.execute(
            select(Match).where(
                Match.adopter_interest_id == interest_id,
                Match.status == "recommended",
            )
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_override_assigns_arbitrary_org_and_flags_match(
    client: TestClient, session: AsyncSession
) -> None:
    """route_elsewhere with facilitator_org_id declines the current match and
    creates a flagged override match + a manual_override audit row."""
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session)
    current = await _make_org_with_coverage(session, people_id3=people_id3)
    m = await _seed_recommended_match(session, contact, current, people_id3)
    target = await _make_org_with_coverage(session, people_id3=people_id3)
    try:
        r = client.post(
            f"/v1/matches/{m.id}/decide",
            json={
                "decision": "route_elsewhere",
                "facilitator_org_id": str(target.id),
            },
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        new = await _open_recommended_match(session, m.adopter_interest_id)
        assert new.facilitator_org_id == target.id
        assert new.is_manual_override is True
        await session.refresh(m)
        assert m.status == "declined"
        attempts = (
            await session.execute(
                select(MatchAttempt).where(
                    MatchAttempt.adopter_interest_id == m.adopter_interest_id,
                    MatchAttempt.candidate_facilitator_id == target.id,
                )
            )
        ).scalars().all()
        override_rows = [
            a
            for a in attempts
            if a.filter_results
            and a.filter_results.get("filter_reason") == "manual_override"
        ]
        assert len(override_rows) == 1
        assert override_rows[0].score is None
        assert override_rows[0].rank is None
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, current.id)
        await _cleanup_org(session, target.id)


@pytest.mark.asyncio
async def test_override_to_no_coverage_org_succeeds(
    client: TestClient, session: AsyncSession
) -> None:
    """Override bypasses the hard filter — an org that does not cover the
    interest's FPG can still be hand-assigned."""
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    other = f"OTH{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session)
    current = await _make_org_with_coverage(session, people_id3=people_id3)
    m = await _seed_recommended_match(session, contact, current, people_id3)
    non_cov = await _make_org_with_coverage(session, people_id3=other)
    try:
        r = client.post(
            f"/v1/matches/{m.id}/decide",
            json={
                "decision": "route_elsewhere",
                "facilitator_org_id": str(non_cov.id),
            },
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        new = await _open_recommended_match(session, m.adopter_interest_id)
        assert new.facilitator_org_id == non_cov.id
        assert new.is_manual_override is True
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, current.id)
        await _cleanup_org(session, non_cov.id)


@pytest.mark.asyncio
async def test_override_at_capacity_then_accept_bypasses_ceiling(
    client: TestClient, session: AsyncSession
) -> None:
    """An override to an at-capacity org can be accepted without a 409, and
    committed never exceeds total (overrides are off the capacity ledger)."""
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session)
    current = await _make_org_with_coverage(session, people_id3=people_id3)
    m = await _seed_recommended_match(session, contact, current, people_id3)
    full = await _make_org_with_coverage(
        session, people_id3=people_id3, capacity_total=2, capacity_committed=2
    )
    try:
        r = client.post(
            f"/v1/matches/{m.id}/decide",
            json={
                "decision": "route_elsewhere",
                "facilitator_org_id": str(full.id),
            },
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        new = await _open_recommended_match(session, m.adopter_interest_id)
        r2 = client.post(
            f"/v1/matches/{new.id}/decide",
            json={"decision": "accept"},
            headers=_auth_headers(),
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["match"]["status"] == "accepted"
        await session.refresh(full)
        # Off-ledger: committed unchanged, never exceeds total.
        assert full.capacity_committed == 2
        assert full.capacity_committed <= full.capacity_total
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, current.id)
        await _cleanup_org(session, full.id)


@pytest.mark.asyncio
async def test_override_is_off_capacity_ledger(
    client: TestClient, session: AsyncSession
) -> None:
    """Override matches never touch capacity_committed — not on accept (an org
    with room is not incremented) and not on a later send_back (no spurious
    decrement). This keeps committed an exact count of non-override
    reservations despite the capacity CHECK forbidding committed > total."""
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session)
    current = await _make_org_with_coverage(session, people_id3=people_id3)
    m = await _seed_recommended_match(session, contact, current, people_id3)
    target = await _make_org_with_coverage(
        session, people_id3=people_id3, capacity_total=5, capacity_committed=1
    )
    try:
        # Override to an org with room.
        assert (
            client.post(
                f"/v1/matches/{m.id}/decide",
                json={
                    "decision": "route_elsewhere",
                    "facilitator_org_id": str(target.id),
                },
                headers=_auth_headers(),
            ).status_code
            == 200
        )
        new = await _open_recommended_match(session, m.adopter_interest_id)
        # Accept: committed stays at 1 (NOT incremented to 2) — off-ledger.
        assert (
            client.post(
                f"/v1/matches/{new.id}/decide",
                json={"decision": "accept"},
                headers=_auth_headers(),
            ).status_code
            == 200
        )
        await session.refresh(target)
        assert target.capacity_committed == 1
        # Send the accepted override back: still no decrement — committed holds.
        accepted = (
            await session.execute(
                select(Match).where(
                    Match.adopter_interest_id == m.adopter_interest_id,
                    Match.status == "accepted",
                )
            )
        ).scalar_one()
        assert (
            client.post(
                f"/v1/matches/{accepted.id}/decide",
                json={"decision": "send_back"},
                headers=_auth_headers(),
            ).status_code
            == 200
        )
        await session.refresh(target)
        assert target.capacity_committed == 1
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, current.id)
        await _cleanup_org(session, target.id)


@pytest.mark.asyncio
async def test_non_override_accept_at_capacity_returns_409(
    client: TestClient, session: AsyncSession
) -> None:
    """The capacity ceiling is preserved for a normal (non-override) accept."""
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session)
    org = await _make_org_with_coverage(
        session, people_id3=people_id3, capacity_total=1, capacity_committed=1
    )
    m = await _seed_recommended_match(session, contact, org, people_id3)
    try:
        r = client.post(
            f"/v1/matches/{m.id}/decide",
            json={"decision": "accept"},
            headers=_auth_headers(),
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "capacity_unavailable"
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, org.id)


@pytest.mark.asyncio
async def test_override_invalid_org_returns_400(
    client: TestClient, session: AsyncSession
) -> None:
    """Override to a triage / inactive / unknown org is rejected 400."""
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session)
    current = await _make_org_with_coverage(session, people_id3=people_id3)
    m = await _seed_recommended_match(session, contact, current, people_id3)
    try:
        # Seeded triage org (bbb1)
        r = client.post(
            f"/v1/matches/{m.id}/decide",
            json={
                "decision": "route_elsewhere",
                "facilitator_org_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1",
            },
            headers=_auth_headers(),
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["code"] == "invalid_override_org"
        # Unknown org id
        r2 = client.post(
            f"/v1/matches/{m.id}/decide",
            json={
                "decision": "route_elsewhere",
                "facilitator_org_id": str(uuid.uuid4()),
            },
            headers=_auth_headers(),
        )
        assert r2.status_code == 400, r2.text
        assert r2.json()["detail"]["code"] == "invalid_override_org"
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, current.id)


@pytest.mark.asyncio
async def test_override_field_validation_422(
    client: TestClient, session: AsyncSession
) -> None:
    """facilitator_org_id is route_elsewhere-only and mutually exclusive with
    next_attempt_id."""
    people_id3 = f"TST{uuid.uuid4().hex[:5].upper()}"
    contact = await _make_contact(session)
    org = await _make_org_with_coverage(session, people_id3=people_id3)
    m = await _seed_recommended_match(session, contact, org, people_id3)
    try:
        # both fields together
        r = client.post(
            f"/v1/matches/{m.id}/decide",
            json={
                "decision": "route_elsewhere",
                "facilitator_org_id": str(uuid.uuid4()),
                "next_attempt_id": str(uuid.uuid4()),
            },
            headers=_auth_headers(),
        )
        assert r.status_code == 422, r.text
        # facilitator_org_id on a non-route_elsewhere verb
        r2 = client.post(
            f"/v1/matches/{m.id}/decide",
            json={
                "decision": "accept",
                "facilitator_org_id": str(uuid.uuid4()),
            },
            headers=_auth_headers(),
        )
        assert r2.status_code == 422, r2.text
    finally:
        await _cleanup_contact_chain(session, contact)
        await _cleanup_org(session, org.id)
