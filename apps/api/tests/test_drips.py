"""U10 drip engine tests.

Coverage:
  * Pure-function: email_hash, template render, suppression check.
  * Domain: enroll_on_event idempotency, do_not_engage exit, suppression
    filter, completion + advance.
  * Step-due: claim_due_steps respects delay_days, state filter, SKIP
    LOCKED (single-tick).
  * API router: campaign CRUD happy path, activate/pause, manual enroll.
  * Worker tick (via _process_due_steps): full chain matched→enroll→step-0
    send + EnrollmentEvent + advance.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

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
from jp_adopt_api.domain.drips import (
    EXIT_REASON_DO_NOT_ENGAGE,
    EXIT_REASON_SUPPRESSED,
    EnrollmentOutcome,
    TemplateMissingError,
    add_to_suppression_list,
    advance_enrollment,
    claim_due_steps,
    email_hash,
    enroll_contact_in_campaign,
    enroll_on_event,
    exit_enrollment,
    exit_enrollments_for_contact,
    is_suppressed,
    render_step_html,
)
from jp_adopt_api.main import app
from jp_adopt_api.models import (
    Campaign,
    CampaignStep,
    Contact,
    Enrollment,
    EnrollmentEvent,
    SuppressionList,
    TransitionAudit,
)

os.environ.setdefault("STRICT_AUTH", "false")
os.environ.setdefault("APP_ENV", "development")
get_settings.cache_clear()


# ─── fixtures ───────────────────────────────────────────────────────────────


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
    email: str | None = None,
    adopter_status: str = "new",
) -> Contact:
    contact = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name="Drip Test Adopter",
        adopter_status=adopter_status,
        email_normalized=email or f"drip-{uuid.uuid4().hex[:10]}@example.com",
    )
    session.add(contact)
    await session.flush()
    await session.commit()
    return contact


async def _make_campaign(
    session: AsyncSession,
    *,
    name: str = "Test Campaign",
    status: str = "active",
    trigger_event_type: str = "jp.adopt.v1.match.accepted_by_facilitator",
    auto_enroll_existing: bool = False,
) -> Campaign:
    campaign = Campaign(
        id=uuid.uuid4(),
        name=name,
        description=None,
        status=status,
        trigger_type="event",
        trigger_event_type=trigger_event_type,
        auto_enroll_existing=auto_enroll_existing,
        version=1,
    )
    session.add(campaign)
    await session.flush()
    await session.commit()
    return campaign


async def _make_step(
    session: AsyncSession,
    campaign: Campaign,
    *,
    position: int = 0,
    delay_days: int = 0,
    template_name: str = "facilitator-welcome.step-0.mjml",
    subject: str = "Welcome",
    send_at_hour: int = 0,
) -> CampaignStep:
    step = CampaignStep(
        id=uuid.uuid4(),
        campaign_id=campaign.id,
        position=position,
        delay_days=delay_days,
        mjml_template_name=template_name,
        subject=subject,
        send_at_hour=send_at_hour,
        send_at_minute=0,
    )
    session.add(step)
    await session.flush()
    await session.commit()
    return step


async def _cleanup_campaign(session: AsyncSession, campaign: Campaign) -> None:
    # Drop child enrollment_event rows via cascade on enrollment, then
    # enrollments, then steps, then the campaign itself.
    enrollment_ids = (
        await session.execute(
            select(Enrollment.id).where(Enrollment.campaign_id == campaign.id)
        )
    ).scalars().all()
    if enrollment_ids:
        await session.execute(
            delete(EnrollmentEvent).where(
                EnrollmentEvent.enrollment_id.in_(enrollment_ids)
            )
        )
        await session.execute(
            delete(Enrollment).where(Enrollment.id.in_(enrollment_ids))
        )
    await session.execute(
        delete(CampaignStep).where(CampaignStep.campaign_id == campaign.id)
    )
    await session.execute(delete(Campaign).where(Campaign.id == campaign.id))
    await session.commit()


async def _cleanup_contact(session: AsyncSession, contact: Contact) -> None:
    await session.execute(
        delete(TransitionAudit).where(TransitionAudit.contact_id == contact.id)
    )
    await session.execute(delete(Contact).where(Contact.id == contact.id))
    await session.commit()


# ─── pure-function ──────────────────────────────────────────────────────────


def test_email_hash_is_stable_and_normalized() -> None:
    assert email_hash("alice@example.com") == email_hash("Alice@Example.COM")


def test_render_step_uses_jinja2_substitution(tmp_path) -> None:
    template_path = tmp_path / "hello.mjml"
    template_path.write_text(
        "<html><body>Hello {{ name }} — {{ campaign_name }}</body></html>"
    )
    html, plain = render_step_html(
        template_name="hello.mjml",
        context={"name": "Alice", "campaign_name": "Welcome"},
        templates_dir=tmp_path,
    )
    assert "Hello Alice" in html
    assert "Welcome" in html
    # Plain text strips tags
    assert "<html>" not in plain
    assert "Hello Alice" in plain


def test_render_step_missing_template_raises(tmp_path) -> None:
    with pytest.raises(TemplateMissingError):
        render_step_html(
            template_name="not-there.mjml",
            context={},
            templates_dir=tmp_path,
        )


# ─── suppression ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_suppression_round_trip(session: AsyncSession) -> None:
    email = f"supp-{uuid.uuid4().hex[:10]}@example.com"
    try:
        assert await is_suppressed(session, email) is False
        await add_to_suppression_list(session, email=email, reason="hard_bounce")
        await session.commit()
        assert await is_suppressed(session, email) is True
        # Idempotent re-add (no exception)
        await add_to_suppression_list(session, email=email, reason="hard_bounce")
        await session.commit()
    finally:
        await session.execute(
            delete(SuppressionList).where(
                SuppressionList.email_hash == email_hash(email)
            )
        )
        await session.commit()


# ─── enrollment ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enroll_on_event_creates_enrollment_when_campaign_matches(
    session: AsyncSession,
) -> None:
    event_type = f"jp.adopt.v1.test.enroll-{uuid.uuid4().hex}"
    contact = await _make_contact(session)
    campaign = await _make_campaign(session, trigger_event_type=event_type)
    await _make_step(session, campaign, position=0)
    try:
        outcomes = await enroll_on_event(
            session,
            event_type=event_type,
            contact_id=contact.id,
        )
        await session.commit()
        assert len(outcomes) == 1
        assert outcomes[0].reason == "created"
        assert outcomes[0].enrollment_id is not None
        # Idempotent re-trigger
        outcomes2 = await enroll_on_event(
            session,
            event_type=event_type,
            contact_id=contact.id,
        )
        await session.commit()
        assert outcomes2[0].reason == "already_enrolled"
        # Only one open enrollment exists
        rows = (
            await session.execute(
                select(Enrollment).where(
                    Enrollment.campaign_id == campaign.id,
                    Enrollment.contact_id == contact.id,
                )
            )
        ).scalars().all()
        assert len(rows) == 1
    finally:
        await _cleanup_campaign(session, campaign)
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_enroll_on_event_no_campaign_matches(
    session: AsyncSession,
) -> None:
    contact = await _make_contact(session)
    try:
        outcomes = await enroll_on_event(
            session,
            event_type="jp.adopt.v1.match.never_triggered",
            contact_id=contact.id,
        )
        assert outcomes == [EnrollmentOutcome(None, "no_campaign")]
    finally:
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_enroll_blocked_when_do_not_engage(
    session: AsyncSession,
) -> None:
    contact = await _make_contact(session, adopter_status="do_not_engage")
    campaign = await _make_campaign(session)
    await _make_step(session, campaign)
    try:
        outcomes = await enroll_on_event(
            session,
            event_type=campaign.trigger_event_type,
            contact_id=contact.id,
        )
        assert outcomes[0].reason == "do_not_engage"
    finally:
        await _cleanup_campaign(session, campaign)
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_enroll_blocked_when_email_suppressed(
    session: AsyncSession,
) -> None:
    email = f"sup-{uuid.uuid4().hex[:10]}@example.com"
    contact = await _make_contact(session, email=email)
    campaign = await _make_campaign(session)
    await _make_step(session, campaign)
    await add_to_suppression_list(session, email=email, reason="manual")
    await session.commit()
    try:
        outcomes = await enroll_on_event(
            session,
            event_type=campaign.trigger_event_type,
            contact_id=contact.id,
        )
        assert outcomes[0].reason == "suppressed"
    finally:
        await session.execute(
            delete(SuppressionList).where(
                SuppressionList.email_hash == email_hash(email)
            )
        )
        await _cleanup_campaign(session, campaign)
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_exit_enrollments_for_contact_terminates_open_only(
    session: AsyncSession,
) -> None:
    contact = await _make_contact(session)
    campaign_a = await _make_campaign(session, name="A")
    await _make_step(session, campaign_a)
    campaign_b = await _make_campaign(session, name="B")
    await _make_step(session, campaign_b)
    try:
        await enroll_contact_in_campaign(
            session, campaign=campaign_a, contact=contact
        )
        await enroll_contact_in_campaign(
            session, campaign=campaign_b, contact=contact
        )
        await session.commit()

        exited = await exit_enrollments_for_contact(
            session, contact_id=contact.id, reason="do_not_engage"
        )
        await session.commit()
        assert exited == 2
        states = (
            await session.execute(
                select(Enrollment.state).where(
                    Enrollment.contact_id == contact.id
                )
            )
        ).scalars().all()
        assert all(s == "exited" for s in states)
    finally:
        await _cleanup_campaign(session, campaign_a)
        await _cleanup_campaign(session, campaign_b)
        await _cleanup_contact(session, contact)


# ─── step-due query ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_due_steps_returns_step_0_immediately(
    session: AsyncSession,
) -> None:
    contact = await _make_contact(session)
    campaign = await _make_campaign(session)
    step = await _make_step(session, campaign, position=0, delay_days=0)
    outcome = await enroll_contact_in_campaign(
        session, campaign=campaign, contact=contact
    )
    await session.commit()
    try:
        due = await claim_due_steps(session)
        # FOR UPDATE SKIP LOCKED requires the row not be locked by another
        # tx; we're the only session.
        assert any(d.enrollment.id == outcome.enrollment_id for d in due)
        ours = next(
            d for d in due if d.enrollment.id == outcome.enrollment_id
        )
        assert ours.step.id == step.id
    finally:
        await _cleanup_campaign(session, campaign)
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_claim_due_steps_respects_delay_days(
    session: AsyncSession,
) -> None:
    """A step with delay_days=3 isn't due until 3 days after the prior
    step (or enrollment for position 0). Step 0 with delay=0 is due
    immediately; step 1 with delay=3 is NOT."""
    contact = await _make_contact(session)
    campaign = await _make_campaign(session)
    await _make_step(session, campaign, position=0, delay_days=0)
    await _make_step(session, campaign, position=1, delay_days=3)
    outcome = await enroll_contact_in_campaign(
        session, campaign=campaign, contact=contact
    )
    await session.commit()
    try:
        # Simulate step 0 having just sent
        enrollment = await session.get(Enrollment, outcome.enrollment_id)
        assert enrollment is not None
        enrollment.current_step_position = 1
        enrollment.last_step_sent_at = datetime.now(UTC)
        await session.commit()

        # Now run the claim — step 1's delay hasn't elapsed
        due = await claim_due_steps(session)
        assert not any(
            d.enrollment.id == outcome.enrollment_id for d in due
        )

        # Move the clock forward by simulating last_step_sent_at = 4 days ago
        enrollment.last_step_sent_at = datetime.now(UTC) - timedelta(days=4)
        await session.commit()
        due = await claim_due_steps(session)
        assert any(d.enrollment.id == outcome.enrollment_id for d in due)
    finally:
        await _cleanup_campaign(session, campaign)
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_claim_due_steps_skips_paused_and_exited(
    session: AsyncSession,
) -> None:
    contact = await _make_contact(session)
    campaign = await _make_campaign(session)
    await _make_step(session, campaign)
    outcome = await enroll_contact_in_campaign(
        session, campaign=campaign, contact=contact
    )
    await session.commit()
    enrollment = await session.get(Enrollment, outcome.enrollment_id)
    assert enrollment is not None
    try:
        # Pause: shouldn't be claimed
        enrollment.state = "paused"
        await session.commit()
        due = await claim_due_steps(session)
        assert not any(
            d.enrollment.id == outcome.enrollment_id for d in due
        )

        # Exit: shouldn't be claimed
        enrollment.state = "exited"
        enrollment.exited_at = datetime.now(UTC)
        enrollment.exit_reason = "manual"
        await session.commit()
        due = await claim_due_steps(session)
        assert not any(
            d.enrollment.id == outcome.enrollment_id for d in due
        )
    finally:
        await _cleanup_campaign(session, campaign)
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_advance_enrollment_marks_completed_on_last_step(
    session: AsyncSession,
) -> None:
    contact = await _make_contact(session)
    campaign = await _make_campaign(session)
    await _make_step(session, campaign, position=0)
    outcome = await enroll_contact_in_campaign(
        session, campaign=campaign, contact=contact
    )
    await session.commit()
    try:
        enrollment = await session.get(Enrollment, outcome.enrollment_id)
        assert enrollment is not None
        advanced = await advance_enrollment(
            session, enrollment, sent_at=datetime.now(UTC)
        )
        await session.commit()
        assert advanced is False
        assert enrollment.state == "completed"
    finally:
        await _cleanup_campaign(session, campaign)
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_advance_enrollment_walks_position_gaps(
    session: AsyncSession,
) -> None:
    """A campaign authored with steps [0,1,2,3] whose middle step (position
    2) was deleted mid-flight must let an enrollment currently at
    position 1 advance to position 3 — not be marked completed because
    position+1 happens to be missing."""
    contact = await _make_contact(session)
    campaign = await _make_campaign(session)
    await _make_step(session, campaign, position=0)
    await _make_step(session, campaign, position=1)
    # Skip position 2 entirely (simulates a deleted middle step).
    await _make_step(session, campaign, position=3)
    outcome = await enroll_contact_in_campaign(
        session, campaign=campaign, contact=contact
    )
    await session.commit()
    try:
        enrollment = await session.get(Enrollment, outcome.enrollment_id)
        assert enrollment is not None
        enrollment.current_step_position = 1
        await session.commit()

        advanced = await advance_enrollment(
            session, enrollment, sent_at=datetime.now(UTC)
        )
        await session.commit()
        assert advanced is True
        assert enrollment.current_step_position == 3
        assert enrollment.state == "active"
    finally:
        await _cleanup_campaign(session, campaign)
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_claim_due_steps_walks_gap_when_current_step_deleted(
    session: AsyncSession,
) -> None:
    """An enrollment at current=2 whose step 2 was deleted must still
    get claimed against the next-higher-position step (e.g. position 3)
    on the next claim cycle, rather than being silently stranded."""
    contact = await _make_contact(session)
    campaign = await _make_campaign(session)
    await _make_step(session, campaign, position=0)
    await _make_step(session, campaign, position=1)
    # No step at position 2.
    step_3 = await _make_step(session, campaign, position=3)
    outcome = await enroll_contact_in_campaign(
        session, campaign=campaign, contact=contact
    )
    await session.commit()
    try:
        enrollment = await session.get(Enrollment, outcome.enrollment_id)
        assert enrollment is not None
        enrollment.current_step_position = 2
        # No prior send → null last_step_sent_at means due immediately.
        enrollment.last_step_sent_at = None
        await session.commit()

        due = await claim_due_steps(session)
        ours = [d for d in due if d.enrollment.id == outcome.enrollment_id]
        assert len(ours) == 1
        assert ours[0].step.id == step_3.id
        assert ours[0].step.position == 3
    finally:
        await _cleanup_campaign(session, campaign)
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_enroll_uses_lowest_step_position_when_steps_skip_zero(
    session: AsyncSession,
) -> None:
    """A campaign whose only steps are [1,2,3] must enroll a contact at
    current_step_position=1, not the hardcoded 0 that would never match
    any step."""
    contact = await _make_contact(session)
    campaign = await _make_campaign(session)
    await _make_step(session, campaign, position=1)
    await _make_step(session, campaign, position=2)
    await _make_step(session, campaign, position=3)
    try:
        outcome = await enroll_contact_in_campaign(
            session, campaign=campaign, contact=contact
        )
        await session.commit()
        assert outcome.reason == "created"
        enrollment = await session.get(Enrollment, outcome.enrollment_id)
        assert enrollment is not None
        assert enrollment.current_step_position == 1
    finally:
        await _cleanup_campaign(session, campaign)
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_exit_enrollment_is_idempotent(session: AsyncSession) -> None:
    contact = await _make_contact(session)
    campaign = await _make_campaign(session)
    await _make_step(session, campaign)
    outcome = await enroll_contact_in_campaign(
        session, campaign=campaign, contact=contact
    )
    await session.commit()
    try:
        enrollment = await session.get(Enrollment, outcome.enrollment_id)
        assert enrollment is not None
        await exit_enrollment(
            session, enrollment, reason=EXIT_REASON_DO_NOT_ENGAGE
        )
        await session.commit()
        assert enrollment.state == "exited"
        # Re-exit: no-op (no new event row)
        events_before = (
            await session.execute(
                select(EnrollmentEvent).where(
                    EnrollmentEvent.enrollment_id == enrollment.id
                )
            )
        ).scalars().all()
        await exit_enrollment(
            session, enrollment, reason=EXIT_REASON_SUPPRESSED
        )
        await session.commit()
        events_after = (
            await session.execute(
                select(EnrollmentEvent).where(
                    EnrollmentEvent.enrollment_id == enrollment.id
                )
            )
        ).scalars().all()
        assert len(events_before) == len(events_after)
        assert enrollment.exit_reason == EXIT_REASON_DO_NOT_ENGAGE
    finally:
        await _cleanup_campaign(session, campaign)
        await _cleanup_contact(session, contact)


# ─── HTTP router ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_and_activate_campaign_via_http(
    client: TestClient, session: AsyncSession
) -> None:
    r = client.post(
        "/v1/drips/campaigns",
        json={
            "name": "Welcome",
            "trigger_type": "event",
            "trigger_event_type": "jp.adopt.v1.match.accepted_by_facilitator",
        },
        headers=_auth_headers(),
    )
    assert r.status_code == 201, r.text
    campaign_id = r.json()["id"]
    try:
        # Can't activate without steps
        r = client.post(
            f"/v1/drips/campaigns/{campaign_id}/activate",
            headers=_auth_headers(),
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "no_steps"

        # Add a step
        r = client.post(
            f"/v1/drips/campaigns/{campaign_id}/steps",
            json={
                "position": 0,
                "delay_days": 0,
                "mjml_template_name": "facilitator-welcome.step-0.mjml",
                "subject": "Welcome",
            },
            headers=_auth_headers(),
        )
        assert r.status_code == 201, r.text

        # Activate
        r = client.post(
            f"/v1/drips/campaigns/{campaign_id}/activate",
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "active"

        # Pause
        r = client.post(
            f"/v1/drips/campaigns/{campaign_id}/pause",
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "paused"
    finally:
        campaign = await session.get(Campaign, uuid.UUID(campaign_id))
        if campaign:
            await _cleanup_campaign(session, campaign)


@pytest.mark.asyncio
async def test_trigger_event_type_required_when_trigger_type_event(
    client: TestClient,
) -> None:
    r = client.post(
        "/v1/drips/campaigns",
        json={"name": "Missing trigger", "trigger_type": "event"},
        headers=_auth_headers(),
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "trigger_event_type_required"


@pytest.mark.asyncio
async def test_manual_enroll_via_http(
    client: TestClient, session: AsyncSession
) -> None:
    contact = await _make_contact(session)
    campaign = await _make_campaign(session, status="active")
    await _make_step(session, campaign)
    try:
        r = client.post(
            f"/v1/drips/campaigns/{campaign.id}/enroll",
            json={"contact_id": str(contact.id)},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reason"] == "created"
        assert body["enrollment_id"] is not None
    finally:
        await _cleanup_campaign(session, campaign)
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_manual_enroll_rejects_inactive_campaign(
    client: TestClient, session: AsyncSession
) -> None:
    contact = await _make_contact(session)
    campaign = await _make_campaign(session, status="draft")
    try:
        r = client.post(
            f"/v1/drips/campaigns/{campaign.id}/enroll",
            json={"contact_id": str(contact.id)},
            headers=_auth_headers(),
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "campaign_inactive"
    finally:
        await _cleanup_campaign(session, campaign)
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_archive_campaign_blocks_when_active_enrollments_exist(
    client: TestClient, session: AsyncSession
) -> None:
    """Archiving mid-flight would orphan the worker's send loop; the
    handler must 409 with a clear code so the UI can prompt the operator
    to pause or exit the open enrollments first."""
    contact = await _make_contact(session)
    campaign = await _make_campaign(session, status="active")
    await _make_step(session, campaign)
    outcome = await enroll_contact_in_campaign(
        session, campaign=campaign, contact=contact
    )
    await session.commit()
    assert outcome.enrollment_id is not None
    try:
        r = client.delete(
            f"/v1/drips/campaigns/{campaign.id}",
            headers=_auth_headers(),
        )
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "campaign_has_active_enrollments"
        assert detail["active_count"] == 1
        # Campaign was not flipped to archived.
        await session.refresh(campaign)
        assert campaign.status == "active"
    finally:
        await _cleanup_campaign(session, campaign)
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_archive_campaign_succeeds_when_no_active_enrollments(
    client: TestClient, session: AsyncSession
) -> None:
    """A campaign whose only open enrollments have been exited (or which
    has none at all) archives normally."""
    contact = await _make_contact(session)
    campaign = await _make_campaign(session, status="active")
    await _make_step(session, campaign)
    outcome = await enroll_contact_in_campaign(
        session, campaign=campaign, contact=contact
    )
    await session.commit()
    # Exit the enrollment so only non-active rows remain.
    enrollment = await session.get(Enrollment, outcome.enrollment_id)
    assert enrollment is not None
    await exit_enrollment(
        session, enrollment, reason=EXIT_REASON_DO_NOT_ENGAGE
    )
    await session.commit()
    try:
        r = client.delete(
            f"/v1/drips/campaigns/{campaign.id}",
            headers=_auth_headers(),
        )
        assert r.status_code == 204, r.text
        await session.refresh(campaign)
        assert campaign.status == "archived"
    finally:
        await _cleanup_campaign(session, campaign)
        await _cleanup_contact(session, contact)


# ─── worker integration: full send tick ────────────────────────────────────


@pytest.mark.asyncio
async def test_worker_tick_renders_and_advances_enrollment(
    session: AsyncSession,
) -> None:
    """End-to-end through the worker's _process_due_steps: enroll a
    contact, run one tick with send_at_hour=0 + ACS unset, verify the
    enrollment advances (completes) and an EnrollmentEvent was logged."""
    from jp_adopt_worker.tasks.send_drip_step import _process_due_steps

    contact = await _make_contact(session)
    campaign = await _make_campaign(session, status="active")
    await _make_step(session, campaign, position=0, send_at_hour=0)
    outcome = await enroll_contact_in_campaign(
        session, campaign=campaign, contact=contact
    )
    await session.commit()
    try:
        counts = await _process_due_steps(
            session,
            acs_connection_string=None,  # dev fallback path
            acs_sender_address="no-reply@example.com",
            now=datetime.now(UTC),
        )
        await session.commit()
        assert counts["sent"] == 1
        enrollment = await session.get(Enrollment, outcome.enrollment_id)
        assert enrollment is not None
        assert enrollment.state == "completed"
        events = (
            await session.execute(
                select(EnrollmentEvent).where(
                    EnrollmentEvent.enrollment_id == enrollment.id
                )
            )
        ).scalars().all()
        event_types = {e.event_type for e in events}
        assert "step_sent" in event_types
    finally:
        await _cleanup_campaign(session, campaign)
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_worker_tick_renders_body_html_step(
    session: AsyncSession,
) -> None:
    """A step authored in-app (body_html set) sends through the worker tick.
    The template name points at a NON-EXISTENT file, so a successful send proves
    the worker used body_html, not the template fallback."""
    from jp_adopt_worker.tasks.send_drip_step import _process_due_steps

    contact = await _make_contact(session)
    campaign = await _make_campaign(session, status="active")
    step = await _make_step(
        session, campaign, position=0, send_at_hour=0,
        template_name="does-not-exist.mjml",
    )
    step.body_html = "<p>Hi {{ contact_display_name }}</p>"
    await session.commit()
    outcome = await enroll_contact_in_campaign(
        session, campaign=campaign, contact=contact
    )
    await session.commit()
    try:
        counts = await _process_due_steps(
            session,
            acs_connection_string=None,
            acs_sender_address="no-reply@example.com",
            now=datetime.now(UTC),
        )
        await session.commit()
        assert counts["sent"] == 1
        enrollment = await session.get(Enrollment, outcome.enrollment_id)
        assert enrollment is not None and enrollment.state == "completed"
    finally:
        await _cleanup_campaign(session, campaign)
        await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_worker_tick_isolates_unrenderable_step(
    session: AsyncSession,
) -> None:
    """A step with no content source (both body_html and template null) raises
    at render. The worker must exit just that enrollment, NOT crash the whole
    batch tick (which would roll back siblings and re-fire forever)."""
    from jp_adopt_worker.tasks.send_drip_step import _process_due_steps

    contact = await _make_contact(session)
    campaign = await _make_campaign(session, status="active")
    step = await _make_step(session, campaign, position=0, send_at_hour=0)
    step.mjml_template_name = None
    step.body_html = None
    await session.commit()
    outcome = await enroll_contact_in_campaign(
        session, campaign=campaign, contact=contact
    )
    await session.commit()
    try:
        # Must not raise.
        counts = await _process_due_steps(
            session,
            acs_connection_string=None,
            acs_sender_address="no-reply@example.com",
            now=datetime.now(UTC),
        )
        await session.commit()
        assert counts["exited"] == 1
        assert counts["sent"] == 0
        enrollment = await session.get(Enrollment, outcome.enrollment_id)
        assert enrollment is not None and enrollment.state == "exited"
    finally:
        await _cleanup_campaign(session, campaign)
        await _cleanup_contact(session, contact)


# ─── Step PATCH + reorder ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_step_updates_fields(
    client: TestClient, session: AsyncSession
) -> None:
    campaign = await _make_campaign(session, name="Step Edit")
    await _make_step(
        session, campaign, position=0, subject="Old", delay_days=3
    )
    try:
        r = client.patch(
            f"/v1/drips/campaigns/{campaign.id}/steps/0",
            json={"subject": "New", "delay_days": 7, "send_at_hour": 14},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["subject"] == "New"
        assert body["delay_days"] == 7
        assert body["send_at_hour"] == 14
        assert body["position"] == 0
    finally:
        await _cleanup_campaign(session, campaign)


@pytest.mark.asyncio
async def test_patch_step_404_when_position_missing(
    client: TestClient, session: AsyncSession
) -> None:
    campaign = await _make_campaign(session)
    try:
        r = client.patch(
            f"/v1/drips/campaigns/{campaign.id}/steps/0",
            json={"subject": "x"},
            headers=_auth_headers(),
        )
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["code"] == "step_not_found"
    finally:
        await _cleanup_campaign(session, campaign)


@pytest.mark.asyncio
async def test_patch_step_moves_to_empty_position(
    client: TestClient, session: AsyncSession
) -> None:
    campaign = await _make_campaign(session)
    step_a = await _make_step(
        session, campaign, position=0, subject="A"
    )
    try:
        r = client.patch(
            f"/v1/drips/campaigns/{campaign.id}/steps/0",
            json={"position": 5},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        assert r.json()["position"] == 5
        # Verify in the DB.
        await session.refresh(step_a)
        assert step_a.position == 5
    finally:
        await _cleanup_campaign(session, campaign)


@pytest.mark.asyncio
async def test_patch_step_swaps_with_occupied_position(
    client: TestClient, session: AsyncSession
) -> None:
    campaign = await _make_campaign(session)
    step_a = await _make_step(
        session, campaign, position=0, subject="A"
    )
    step_b = await _make_step(
        session, campaign, position=1, subject="B"
    )
    try:
        r = client.patch(
            f"/v1/drips/campaigns/{campaign.id}/steps/0",
            json={"position": 1},
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["subject"] == "A"
        assert body["position"] == 1
        # After swap: A at 1, B at 0.
        await session.refresh(step_a)
        await session.refresh(step_b)
        assert step_a.position == 1
        assert step_b.position == 0
    finally:
        await _cleanup_campaign(session, campaign)


@pytest.mark.asyncio
async def test_patch_step_rejects_capacity_committed_no_unknown_fields(
    client: TestClient, session: AsyncSession
) -> None:
    """extra='forbid' rejects unrecognized fields with 422."""
    campaign = await _make_campaign(session)
    await _make_step(session, campaign, position=0)
    try:
        r = client.patch(
            f"/v1/drips/campaigns/{campaign.id}/steps/0",
            json={"bogus_field": "x"},
            headers=_auth_headers(),
        )
        assert r.status_code == 422, r.text
    finally:
        await _cleanup_campaign(session, campaign)


# ─── Step preview (#55 v2 stretch): POST /campaigns/{id}/steps/{pos}/preview ──


@pytest.mark.asyncio
async def test_preview_step_renders_branded_html(
    client: TestClient, session: AsyncSession
) -> None:
    campaign = await _make_campaign(session, name="Preview Test")
    await _make_step(
        session,
        campaign,
        position=0,
        subject="Welcome to preview",
        template_name="facilitator-welcome.step-0.mjml",
    )
    try:
        r = client.post(
            f"/v1/drips/campaigns/{campaign.id}/steps/0/preview",
            headers=_auth_headers(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["campaign_id"] == str(campaign.id)
        assert body["position"] == 0
        assert body["subject"] == "Welcome to preview"
        assert body["mjml_template_name"] == "facilitator-welcome.step-0.mjml"
        # Branded chrome + sample-context substitution land in `html`.
        assert "#303030" in body["html"]
        assert "#eb5f1e" in body["html"]
        assert "Alex Smith" in body["html"]
        # Plain text strips tags but keeps content.
        assert "<html" not in body["plain"]
        assert "Alex Smith" in body["plain"]
        # Sample context is echoed for UI surfaces that want to show it.
        assert body["sample_context"]["contact_display_name"] == "Alex Smith"
        assert body["sample_context"]["campaign_name"] == "Preview Test"
    finally:
        await _cleanup_campaign(session, campaign)


@pytest.mark.asyncio
async def test_preview_step_unknown_position_returns_404(
    client: TestClient, session: AsyncSession
) -> None:
    campaign = await _make_campaign(session)
    await _make_step(session, campaign, position=0)
    try:
        r = client.post(
            f"/v1/drips/campaigns/{campaign.id}/steps/99/preview",
            headers=_auth_headers(),
        )
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["code"] == "step_not_found"
    finally:
        await _cleanup_campaign(session, campaign)


@pytest.mark.asyncio
async def test_preview_step_missing_template_returns_404(
    client: TestClient, session: AsyncSession
) -> None:
    campaign = await _make_campaign(session)
    await _make_step(
        session, campaign, position=0, template_name="does-not-exist.mjml"
    )
    try:
        r = client.post(
            f"/v1/drips/campaigns/{campaign.id}/steps/0/preview",
            headers=_auth_headers(),
        )
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["code"] == "template_not_found"
    finally:
        await _cleanup_campaign(session, campaign)


@pytest.mark.asyncio
async def test_preview_step_non_staff_returns_403(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    from jp_adopt_api import deps

    async def _facilitator_only(*args, **kwargs) -> frozenset[str]:
        return frozenset({"facilitator"})

    monkeypatch.setattr(deps, "load_user_roles", _facilitator_only)

    campaign = await _make_campaign(session)
    await _make_step(session, campaign, position=0)
    try:
        r = client.post(
            f"/v1/drips/campaigns/{campaign.id}/steps/0/preview",
            headers=_auth_headers(),
        )
        assert r.status_code == 403, r.text
    finally:
        await _cleanup_campaign(session, campaign)


# ─── F3 (#55): GET /v1/drips/templates ────────────────────────────────────


def test_list_templates_returns_mjml_filenames_sorted(client: TestClient) -> None:
    """The real EMAIL_TEMPLATES_DIR ships with two demo MJML files; the
    endpoint returns them sorted lexicographically. New template additions
    will extend this list but not break the assertion."""
    r = client.get("/v1/drips/templates", headers=_auth_headers())
    assert r.status_code == 200, r.text
    names = [t["name"] for t in r.json()["items"]]
    assert names == sorted(names)
    assert "facilitator-welcome.step-0.mjml" in names
    assert all(n.endswith(".mjml") for n in names)


def test_list_templates_excludes_non_mjml(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A stray .md or .txt file in the templates directory is not surfaced."""
    from jp_adopt_api.routers import drips as drips_router

    (tmp_path / "welcome.mjml").write_text("<mjml></mjml>")
    (tmp_path / "notes.md").write_text("# notes")
    (tmp_path / "readme.txt").write_text("readme")
    monkeypatch.setattr(drips_router, "EMAIL_TEMPLATES_DIR", tmp_path)

    r = client.get("/v1/drips/templates", headers=_auth_headers())
    assert r.status_code == 200, r.text
    assert [t["name"] for t in r.json()["items"]] == ["welcome.mjml"]


def test_list_templates_missing_directory_returns_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A fresh dev environment without the templates directory must not
    500 — the endpoint degrades to an empty list."""
    from jp_adopt_api.routers import drips as drips_router

    monkeypatch.setattr(
        drips_router, "EMAIL_TEMPLATES_DIR", tmp_path / "does-not-exist"
    )
    r = client.get("/v1/drips/templates", headers=_auth_headers())
    assert r.status_code == 200, r.text
    assert r.json() == {"items": []}


@pytest.mark.asyncio
async def test_list_templates_non_staff_returns_403(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pure facilitator (no staff role) is refused — templates are part of
    the campaign-management surface."""
    from jp_adopt_api import deps as deps_module

    async def _fake_roles(db: object, user_sub: str) -> frozenset[str]:
        return frozenset({"facilitator"})

    monkeypatch.setattr(deps_module, "load_user_roles", _fake_roles)
    r = client.get("/v1/drips/templates", headers=_auth_headers())
    assert r.status_code == 403, r.text
