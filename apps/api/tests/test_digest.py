"""U11 daily digest tests."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jp_adopt_api.config import get_settings
from jp_adopt_api.domain.digest import (
    DIGEST_MATCH_STATUSES,
    DigestMatch,
    DigestRecipientPlan,
    build_digest_for_window,
    render_digest_html,
)
from jp_adopt_api.models import (
    AdopterInterest,
    Contact,
    DigestRecipient,
    DigestRun,
    FacilitatingOrg,
    Match,
    Role,
    UserRole,
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


async def _make_contact(
    session: AsyncSession,
    *,
    email: str | None = None,
    b2c_subject_id: str | None = None,
    party_kind: str = "adopter",
    adopter_status: str | None = "new",
) -> Contact:
    contact = Contact(
        id=uuid.uuid4(),
        party_kind=party_kind,
        display_name=f"Digest test {uuid.uuid4().hex[:6]}",
        adopter_status=adopter_status,
        email_normalized=email or f"dgst-{uuid.uuid4().hex[:10]}@example.com",
        b2c_subject_id=b2c_subject_id,
    )
    session.add(contact)
    await session.flush()
    await session.commit()
    return contact


async def _make_org(session: AsyncSession) -> FacilitatingOrg:
    org = FacilitatingOrg(
        id=uuid.uuid4(),
        name=f"DigestOrg {uuid.uuid4().hex[:6]}",
        country_code="US",
        capacity_total=5,
        capacity_committed=0,
        active=True,
        is_triage_org=False,
    )
    session.add(org)
    await session.flush()
    await session.commit()
    return org


async def _make_match_in_window(
    session: AsyncSession,
    *,
    contact: Contact,
    org: FacilitatingOrg,
    recommended_at: datetime,
    people_id3: str | None = None,
    match_status: str = "recommended",
) -> Match:
    interest = AdopterInterest(
        id=uuid.uuid4(), contact_id=contact.id, people_id3=people_id3
    )
    session.add(interest)
    await session.flush()
    match = Match(
        id=uuid.uuid4(),
        adopter_interest_id=interest.id,
        facilitator_org_id=org.id,
        status=match_status,
        recommended_at=recommended_at,
    )
    session.add(match)
    await session.flush()
    await session.commit()
    return match


async def _assign_role(
    session: AsyncSession, *, user_sub: str, role_name: str
) -> None:
    role = (
        await session.execute(
            select(Role).where(Role.name == role_name)
        )
    ).scalar_one()
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


async def _cleanup(session: AsyncSession, contacts: list[Contact]) -> None:
    """Drop everything we created for this test, in FK order."""
    for c in contacts:
        interest_ids = (
            await session.execute(
                select(AdopterInterest.id).where(
                    AdopterInterest.contact_id == c.id
                )
            )
        ).scalars().all()
        if interest_ids:
            await session.execute(
                delete(Match).where(
                    Match.adopter_interest_id.in_(interest_ids)
                )
            )
            await session.execute(
                delete(AdopterInterest).where(
                    AdopterInterest.id.in_(interest_ids)
                )
            )
        if c.b2c_subject_id:
            await session.execute(
                delete(UserRole).where(
                    UserRole.user_subject_id == c.b2c_subject_id
                )
            )
        await session.execute(delete(Contact).where(Contact.id == c.id))
    # Wipe digest_run / digest_recipient rows from this test run.
    await session.execute(delete(DigestRecipient))
    await session.execute(delete(DigestRun))
    await session.commit()


# ─── build_digest_for_window ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_digest_groups_staff_and_facilitator(
    session: AsyncSession,
) -> None:
    """staff_admin gets all matches; a facilitator member of org_a gets
    only org_a's. A facilitator member of an org with no matches gets no
    digest."""
    now = datetime.now(UTC)
    window_start = now - timedelta(hours=24)
    window_end = now + timedelta(hours=1)

    # Staff member (has b2c_subject_id + staff_admin role)
    staff_sub = f"staff-{uuid.uuid4().hex[:8]}"
    staff = await _make_contact(
        session, b2c_subject_id=staff_sub, party_kind="adopter"
    )
    await _assign_role(session, user_sub=staff_sub, role_name="staff_admin")

    # Facilitator member of org_a
    fac_sub = f"fac-{uuid.uuid4().hex[:8]}"
    fac = await _make_contact(
        session, b2c_subject_id=fac_sub, party_kind="facilitator"
    )
    # Adopter who got matched (not a staff/facilitator)
    adopter = await _make_contact(session)

    org_a = await _make_org(session)
    # Membership: fac → org_a
    from jp_adopt_api.models import FacilitatorOrgMembership

    session.add(
        FacilitatorOrgMembership(
            user_subject_id=fac_sub, facilitator_org_id=org_a.id
        )
    )
    await session.commit()

    # One match in window
    match = await _make_match_in_window(
        session,
        contact=adopter,
        org=org_a,
        recommended_at=now - timedelta(hours=1),
        people_id3="AAA01",
    )

    try:
        plans = await build_digest_for_window(
            session, window_start=window_start, window_end=window_end
        )
        addresses = {p.recipient_address for p in plans}
        # Staff is in there
        assert staff.email_normalized in addresses
        # Facilitator member of org_a is in there
        assert fac.email_normalized in addresses
        # Adopter (no role / membership) is NOT
        assert adopter.email_normalized not in addresses

        # Staff sees the match; facilitator sees the same match (only
        # match in their org)
        staff_plan = next(
            p for p in plans if p.recipient_address == staff.email_normalized
        )
        assert any(m.match_id == match.id for m in staff_plan.matches)
        assert staff_plan.recipient_kind == "all_staff"
        fac_plan = next(
            p for p in plans if p.recipient_address == fac.email_normalized
        )
        assert any(m.match_id == match.id for m in fac_plan.matches)
        assert fac_plan.recipient_kind == "facilitator"
        assert fac_plan.facilitator_org_id == org_a.id
    finally:
        await session.execute(
            delete(FacilitatorOrgMembership).where(
                FacilitatorOrgMembership.user_subject_id == fac_sub
            )
        )
        await session.commit()
        await _cleanup(session, [staff, fac, adopter])
        await session.execute(
            delete(FacilitatingOrg).where(FacilitatingOrg.id == org_a.id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_build_digest_empty_window_returns_empty_list(
    session: AsyncSession,
) -> None:
    # Tiny window far in the future — no matches
    window_start = datetime.now(UTC) + timedelta(days=365)
    window_end = window_start + timedelta(hours=1)
    plans = await build_digest_for_window(
        session, window_start=window_start, window_end=window_end
    )
    assert plans == []


@pytest.mark.asyncio
async def test_build_digest_excludes_matches_outside_window(
    session: AsyncSession,
) -> None:
    """Matches older than 24h shouldn't appear in today's digest."""
    now = datetime.now(UTC)
    window_start = now - timedelta(hours=24)
    window_end = now + timedelta(hours=1)

    staff_sub = f"staff-{uuid.uuid4().hex[:8]}"
    staff = await _make_contact(session, b2c_subject_id=staff_sub)
    await _assign_role(session, user_sub=staff_sub, role_name="staff_admin")
    adopter = await _make_contact(session)
    org = await _make_org(session)
    # Old match — 36h ago
    old_match = await _make_match_in_window(
        session,
        contact=adopter,
        org=org,
        recommended_at=now - timedelta(hours=36),
    )
    # Fresh match
    fresh_match = await _make_match_in_window(
        session,
        contact=adopter,
        org=org,
        recommended_at=now - timedelta(hours=2),
    )
    try:
        plans = await build_digest_for_window(
            session, window_start=window_start, window_end=window_end
        )
        staff_plan = next(
            p for p in plans if p.recipient_address == staff.email_normalized
        )
        match_ids = {m.match_id for m in staff_plan.matches}
        assert fresh_match.id in match_ids
        assert old_match.id not in match_ids
    finally:
        await _cleanup(session, [staff, adopter])
        await session.execute(
            delete(FacilitatingOrg).where(FacilitatingOrg.id == org.id)
        )
        await session.commit()


# ─── render ────────────────────────────────────────────────────────────────


def test_render_digest_html_substitutes_recipient_kind() -> None:
    plan = DigestRecipientPlan(
        recipient_address="x@example.com",
        recipient_kind="all_staff",
        facilitator_org_id=None,
        matches=[
            DigestMatch(
                match_id=uuid.uuid4(),
                contact_id=uuid.uuid4(),
                contact_display_name="Alice",
                contact_email_normalized="alice@example.com",
                people_id3="AAA01",
                facilitator_org_id=uuid.uuid4(),
                facilitator_name="Org A",
                status="recommended",
                recommended_at=datetime.now(UTC),
            )
        ],
    )
    html, plain = render_digest_html(plan=plan)
    assert "Today's matches" in html
    assert "Alice" in html
    assert "Org A" in html
    assert "AAA01" in html
    assert "<html>" not in plain


def test_render_digest_html_facilitator_framing() -> None:
    plan = DigestRecipientPlan(
        recipient_address="fac@example.com",
        recipient_kind="facilitator",
        facilitator_org_id=uuid.uuid4(),
        matches=[],
    )
    html, _ = render_digest_html(plan=plan)
    assert "Your org's matches today" in html


# ─── worker idempotency ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_digest_idempotent_on_same_window(
    session: AsyncSession,
) -> None:
    """A second `run_digest` invocation for the same window should not
    re-send."""
    from jp_adopt_worker.tasks.send_daily_digest import run_digest

    now = datetime.now(UTC)
    window_start = now.replace(minute=0, second=0, microsecond=0) - timedelta(
        hours=24
    )
    window_end = window_start + timedelta(hours=24)

    staff_sub = f"staff-{uuid.uuid4().hex[:8]}"
    staff = await _make_contact(session, b2c_subject_id=staff_sub)
    await _assign_role(session, user_sub=staff_sub, role_name="staff_admin")
    adopter = await _make_contact(session)
    org = await _make_org(session)
    await _make_match_in_window(
        session,
        contact=adopter,
        org=org,
        recommended_at=window_start + timedelta(hours=1),
    )

    try:
        # First run: writes a digest_run + digest_recipient row
        counts = await run_digest(
            session,
            window_start=window_start,
            window_end=window_end,
            acs_connection_string=None,
            acs_sender_address="no-reply@example.com",
        )
        await session.commit()
        assert counts["sent"] >= 1
        first_run_count = (
            await session.execute(
                select(DigestRun).where(DigestRun.window_start == window_start)
            )
        ).scalars().all()
        assert len(first_run_count) == 1

        # Second run: skipped (already 'sent')
        counts2 = await run_digest(
            session,
            window_start=window_start,
            window_end=window_end,
            acs_connection_string=None,
            acs_sender_address="no-reply@example.com",
        )
        await session.commit()
        assert counts2["skipped"] == -1

        # Still only one DigestRun row
        second_run_count = (
            await session.execute(
                select(DigestRun).where(DigestRun.window_start == window_start)
            )
        ).scalars().all()
        assert len(second_run_count) == 1
    finally:
        await _cleanup(session, [staff, adopter])
        await session.execute(
            delete(FacilitatingOrg).where(FacilitatingOrg.id == org.id)
        )
        await session.commit()


# ─── eastern hour helper ───────────────────────────────────────────────────


def test_eastern_now_offset_matches_dst_window() -> None:
    """Sanity-check the DST helper. Pre-March: EST (-5). Post-mid-March:
    EDT (-4). Late November: back to EST."""
    from jp_adopt_worker.tasks.send_daily_digest import _eastern_now

    midwinter = datetime(2026, 1, 15, 14, 0, 0, tzinfo=UTC)
    midsummer = datetime(2026, 7, 15, 14, 0, 0, tzinfo=UTC)
    eastern_winter = _eastern_now(midwinter)
    eastern_summer = _eastern_now(midsummer)
    # In winter 14:00 UTC = 09:00 EST; in summer 14:00 UTC = 10:00 EDT.
    assert eastern_winter.hour == 9
    assert eastern_summer.hour == 10


def test_digest_match_statuses_align_with_plan() -> None:
    """The plan says recommended+accepted matches count toward the
    digest. Don't accidentally drop one — this assertion catches a
    regression."""
    assert set(DIGEST_MATCH_STATUSES) == {"recommended", "accepted"}
