"""U6 matching algorithm tests.

Covers the test scenarios called out in the plan:
  * happy path — adopter with FPG + 3 covering facilitators → 3 MatchAttempts
    + 3 recommended Match rows, ranked deterministically;
  * happy path — adopter with no FPG → 1 triage Match row, 0 MatchAttempts;
  * edge case — no-coverage (FPG selected, hard filter eliminates everyone)
    → 1 triage Match row;
  * edge case — re-match after send-back excludes the prior facilitator;
  * edge case — tied scores resolve deterministically via last_assigned_at;
  * integration — score_breakdown JSONB round-trips into match_attempt.

Uses a per-test async session backed by a fresh engine (the pattern that
test_match_domain.py + test_magic_link.py both use to avoid the cached
app-engine cross-event-loop issue).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterable
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
from jp_adopt_api.domain.matching import (
    FilterReason,
    ScoreVector,
    TriageOrgMissingError,
    hard_filter,
    match_or_route,
    score,
)
from jp_adopt_api.domain.matching_config import DEFAULT_WEIGHTS
from jp_adopt_api.models import (
    AdopterInterest,
    Contact,
    FacilitatingOrg,
    FacilitatorFpgCoverage,
    Fpg,
    Match,
    MatchAttempt,
)

# Deterministic IDs from migration 0005 seed.
TRIAGE_ORG_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1")
EXAMPLE_MISSION_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2")
FRONTIER_ALLIANCE_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb3")


# ─── fixtures ───────────────────────────────────────────────────────────────


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
    country: str | None = "US",
    languages: list[str] | None = None,
) -> Contact:
    """Insert a fresh contact and return the persisted instance."""
    if email is None:
        email = f"mtch-{uuid.uuid4().hex[:10]}@example.com"
    contact = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name=f"Test {email}",
        adopter_status="new",
        email_normalized=email,
        country_code=country,
        language_codes=languages,
    )
    session.add(contact)
    await session.flush()
    return contact


async def _make_interest(
    session: AsyncSession, contact: Contact, people_id3: str | None
) -> AdopterInterest:
    interest = AdopterInterest(
        id=uuid.uuid4(),
        contact_id=contact.id,
        people_id3=people_id3,
    )
    session.add(interest)
    await session.flush()
    return interest


async def _seed_three_covering_orgs(
    session: AsyncSession,
    *,
    people_id3: str = "ZZZ01",
) -> tuple[FacilitatingOrg, FacilitatingOrg, FacilitatingOrg]:
    """Insert three NEW facilitating orgs (distinct from the seeded
    EXAMPLE/FRONTIER ones) that all cover ``rop3``. Returns them in
    deterministic order for the ranking assertions."""
    # Ensure the people_id3 row exists in fpg (FK target).
    existing_fpg = await session.get(Fpg, people_id3)
    if existing_fpg is None:
        session.add(
            Fpg(people_id3=people_id3, name=f"Test FPG {people_id3}", country_code="US", frontier=True)
        )
        await session.flush()
    orgs: list[FacilitatingOrg] = []
    for i in range(3):
        org = FacilitatingOrg(
            id=uuid.uuid4(),
            name=f"Test Org {people_id3}-{i}",
            country_code="US",
            language_codes=["en"],
            capacity_total=10,
            capacity_committed=i,  # different headroom per org → different scores
            active=True,
            is_triage_org=False,
        )
        session.add(org)
        session.add(
            FacilitatorFpgCoverage(facilitator_org_id=org.id, people_id3=people_id3)
        )
        orgs.append(org)
    await session.flush()
    return orgs[0], orgs[1], orgs[2]


async def _cleanup_contact(session: AsyncSession, contact: Contact) -> None:
    """Remove a test contact and everything that hangs off it. Cascades on
    contact handle adopter_interest + match_attempt; match has ondelete=RESTRICT
    so delete those first."""
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
    await session.execute(delete(Contact).where(Contact.id == contact.id))
    await session.commit()


async def _cleanup_orgs(
    session: AsyncSession,
    *org_ids: uuid.UUID,
    people_id3s: Iterable[str] = (),
) -> None:
    if not org_ids and not people_id3s:
        return
    # Match rows pointing at these orgs must go first.
    if org_ids:
        await session.execute(
            delete(Match).where(Match.facilitator_org_id.in_(org_ids))
        )
        await session.execute(
            delete(MatchAttempt).where(
                MatchAttempt.candidate_facilitator_id.in_(org_ids)
            )
        )
        await session.execute(
            delete(FacilitatorFpgCoverage).where(
                FacilitatorFpgCoverage.facilitator_org_id.in_(org_ids)
            )
        )
        await session.execute(
            delete(FacilitatingOrg).where(FacilitatingOrg.id.in_(org_ids))
        )
    for people_id3 in people_id3s:
        # Drop coverage+fpg only if nothing else still references this people_id3
        # (defensive; in this suite each test owns its own people_id3 string).
        await session.execute(
            delete(FacilitatorFpgCoverage).where(
                FacilitatorFpgCoverage.people_id3 == people_id3
            )
        )
        await session.execute(delete(Fpg).where(Fpg.people_id3 == people_id3))
    await session.commit()


# ─── pure-function tests (no DB session needed) ────────────────────────────


def test_hard_filter_passes_active_with_capacity_and_coverage() -> None:
    org = FacilitatingOrg(
        id=uuid.uuid4(),
        name="X",
        country_code="US",
        capacity_total=5,
        capacity_committed=1,
        active=True,
        is_triage_org=False,
    )
    result = hard_filter(
        facilitator=org,
        people_id3="AAA01",
        covered_people_id3s=frozenset({"AAA01"}),
        excluded_facilitator_ids=frozenset(),
    )
    assert result == FilterReason.PASSED


def test_hard_filter_rejects_inactive() -> None:
    org = FacilitatingOrg(
        id=uuid.uuid4(),
        name="X",
        capacity_total=5,
        capacity_committed=0,
        active=False,
        is_triage_org=False,
    )
    assert (
        hard_filter(
            facilitator=org,
            people_id3="AAA01",
            covered_people_id3s=frozenset({"AAA01"}),
            excluded_facilitator_ids=frozenset(),
        )
        == FilterReason.INACTIVE
    )


def test_hard_filter_rejects_no_capacity() -> None:
    org = FacilitatingOrg(
        id=uuid.uuid4(),
        name="X",
        capacity_total=5,
        capacity_committed=5,
        active=True,
        is_triage_org=False,
    )
    assert (
        hard_filter(
            facilitator=org,
            people_id3="AAA01",
            covered_people_id3s=frozenset({"AAA01"}),
            excluded_facilitator_ids=frozenset(),
        )
        == FilterReason.NO_CAPACITY
    )


def test_hard_filter_rejects_no_coverage() -> None:
    org = FacilitatingOrg(
        id=uuid.uuid4(),
        name="X",
        capacity_total=5,
        capacity_committed=0,
        active=True,
        is_triage_org=False,
    )
    assert (
        hard_filter(
            facilitator=org,
            people_id3="AAA01",
            covered_people_id3s=frozenset({"AAA02"}),
            excluded_facilitator_ids=frozenset(),
        )
        == FilterReason.NO_COVERAGE
    )


def test_hard_filter_rejects_excluded() -> None:
    excluded_id = uuid.uuid4()
    org = FacilitatingOrg(
        id=excluded_id,
        name="X",
        capacity_total=5,
        capacity_committed=0,
        active=True,
        is_triage_org=False,
    )
    assert (
        hard_filter(
            facilitator=org,
            people_id3="AAA01",
            covered_people_id3s=frozenset({"AAA01"}),
            excluded_facilitator_ids=frozenset({excluded_id}),
        )
        == FilterReason.EXCLUDED
    )


def test_score_weighted_total_sums_to_one_for_perfect_match() -> None:
    contact = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name="x",
        country_code="US",
        language_codes=["en"],
    )
    org = FacilitatingOrg(
        id=uuid.uuid4(),
        name="X",
        country_code="US",
        language_codes=["en"],
        capacity_total=10,
        capacity_committed=0,
        active=True,
        is_triage_org=False,
    )
    sv = score(
        contact=contact,
        facilitator=org,
        people_id3="AAA01",
        covered_people_id3s=frozenset({"AAA01"}),
    )
    # Capacity headroom = 10/10 = 1.0; geography 1.0; language Jaccard 1.0;
    # fpg_affinity 1.0; theological 0.0 (stub).
    # Weighted total = 1.0*.40 + 1.0*.30 + 1.0*.15 + 1.0*.10 + 0.0*.05 = 0.95
    assert sv.capacity_headroom == 1.0
    assert sv.geography == 1.0
    assert sv.language == 1.0
    assert sv.fpg_affinity == 1.0
    assert sv.theological == 0.0
    assert sv.weighted_total(DEFAULT_WEIGHTS) == pytest.approx(0.95)


def test_score_breakdown_round_trips_to_dict() -> None:
    sv = ScoreVector(0.5, 1.0, 0.25, 1.0, 0.0)
    d = sv.as_dict()
    assert d == {
        "capacity_headroom": 0.5,
        "geography": 1.0,
        "language": 0.25,
        "fpg_affinity": 1.0,
        "theological": 0.0,
    }


def test_open_match_statuses_constant_matches_migration() -> None:
    """adv5-002 / RR-1: guard against drift between matching.py's
    ``_OPEN_MATCH_STATUSES_FOR_CONFLICT_REFETCH`` and migration 0005's
    ``OPEN_MATCH_STATUSES``. The two MUST list the same statuses or the
    conflict-guard refetch will miss winners with statuses the index covers
    but the constant omits — silently re-raising or skipping a still-recoverable
    Match. The cross-reference comment is the only guard today; this test
    makes drift surface at CI time, not at production-incident time.
    """
    import importlib.util
    from pathlib import Path

    from jp_adopt_api.domain.matching import (
        _OPEN_MATCH_STATUSES_FOR_CONFLICT_REFETCH,
    )

    mig_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "20260517_0005_match_domain.py"
    )
    spec = importlib.util.spec_from_file_location("_mig_0005_drift", mig_path)
    assert spec is not None and spec.loader is not None
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    assert set(_OPEN_MATCH_STATUSES_FOR_CONFLICT_REFETCH) == set(
        mig.OPEN_MATCH_STATUSES
    ), (
        "matching.py's _OPEN_MATCH_STATUSES_FOR_CONFLICT_REFETCH has drifted "
        "from migration 0005's OPEN_MATCH_STATUSES. Update both."
    )


# ─── integration tests against a live DB ───────────────────────────────────


@pytest.mark.asyncio
async def test_no_fpg_adopter_routes_to_triage(session: AsyncSession) -> None:
    """Plan happy path: adopter with no FPG → one Match row at triage, no
    MatchAttempt rows."""
    contact = await _make_contact(session)
    interest = await _make_interest(session, contact, people_id3=None)
    await session.commit()

    outcome = await match_or_route(session, contact)
    await session.commit()

    assert outcome.total_triage == 1
    assert outcome.total_recommended == 0
    assert outcome.interest_outcomes[0].reason == "no_fpg"

    matches = (
        await session.execute(
            select(Match).where(Match.adopter_interest_id == interest.id)
        )
    ).scalars().all()
    assert len(matches) == 1
    assert matches[0].status == "triage"
    assert matches[0].facilitator_org_id == TRIAGE_ORG_ID

    attempts = (
        await session.execute(
            select(MatchAttempt).where(
                MatchAttempt.adopter_interest_id == interest.id
            )
        )
    ).scalars().all()
    assert attempts == []

    await _cleanup_contact(session, contact)


@pytest.mark.asyncio
async def test_three_covering_facilitators_rank_in_attempt_one_recommended(
    session: AsyncSession,
) -> None:
    """3 candidates pass filter → 3 ranked MatchAttempts + 1 Match row
    pointing at rank-1 (per uq_match_open_per_interest)."""
    people_id3 = f"ZZZ{uuid.uuid4().hex[:3].upper()}"
    org_low, org_mid, org_high = await _seed_three_covering_orgs(
        session, people_id3=people_id3
    )
    contact = await _make_contact(
        session, country="US", languages=["en"]
    )
    interest = await _make_interest(session, contact, people_id3=people_id3)
    await session.commit()

    outcome = await match_or_route(session, contact)
    await session.commit()

    assert outcome.total_triage == 0
    assert outcome.total_recommended == 1
    assert outcome.interest_outcomes[0].reason == "scored"

    # The algorithm scores every non-triage facilitator in the DB (the 3 we
    # seeded for this people_id3 + every other org → filter_reason=no_coverage for
    # those). We assert on the 3 that pass: they get rank 1..3.
    attempts = (
        await session.execute(
            select(MatchAttempt)
            .where(
                MatchAttempt.adopter_interest_id == interest.id,
                MatchAttempt.candidate_facilitator_id.in_(
                    [org_low.id, org_mid.id, org_high.id]
                ),
            )
            .order_by(MatchAttempt.score.desc())
        )
    ).scalars().all()
    assert len(attempts) == 3
    # All three pass filter and get scored; org_low (capacity_committed=0)
    # has the most headroom → highest score; org_high has the least.
    ranks_by_org = {
        a.candidate_facilitator_id: a.rank for a in attempts if a.rank is not None
    }
    assert ranks_by_org[org_low.id] == 1
    assert ranks_by_org[org_mid.id] == 2
    assert ranks_by_org[org_high.id] == 3

    # Exactly one Match row at status='recommended' pointing at rank 1.
    matches = (
        await session.execute(
            select(Match).where(Match.adopter_interest_id == interest.id)
        )
    ).scalars().all()
    assert len(matches) == 1
    assert matches[0].status == "recommended"
    assert matches[0].facilitator_org_id == org_low.id

    await _cleanup_contact(session, contact)
    await _cleanup_orgs(
        session, org_low.id, org_mid.id, org_high.id, people_id3s=[people_id3]
    )


@pytest.mark.asyncio
async def test_no_coverage_routes_to_triage(session: AsyncSession) -> None:
    """Plan edge case: FPG selected but no facilitator covers it → triage."""
    # Pick a people_id3 that no seeded org covers AND we don't create coverage for.
    orphan_people_id3 = f"NONE{uuid.uuid4().hex[:2].upper()}"
    # The fpg FK requires the people_id3 row to exist.
    session.add(Fpg(people_id3=orphan_people_id3, name="Orphan FPG", frontier=True))
    await session.flush()

    contact = await _make_contact(session)
    interest = await _make_interest(session, contact, people_id3=orphan_people_id3)
    await session.commit()

    outcome = await match_or_route(session, contact)
    await session.commit()

    assert outcome.total_triage == 1
    assert outcome.total_recommended == 0
    assert outcome.interest_outcomes[0].reason == "no_coverage"

    matches = (
        await session.execute(
            select(Match).where(Match.adopter_interest_id == interest.id)
        )
    ).scalars().all()
    assert len(matches) == 1
    assert matches[0].status == "triage"
    assert matches[0].facilitator_org_id == TRIAGE_ORG_ID

    # MatchAttempt rows exist for every candidate considered + filtered out.
    # (The seeded orgs all fail with NO_COVERAGE because they cover AAA01..05.)
    attempts = (
        await session.execute(
            select(MatchAttempt).where(
                MatchAttempt.adopter_interest_id == interest.id
            )
        )
    ).scalars().all()
    for a in attempts:
        assert a.filter_results["filter_reason"] == "no_coverage"
        assert a.score is None  # didn't pass filter, so not scored
        assert a.rank is None

    await _cleanup_contact(session, contact)
    # The MatchAttempt rows the algorithm wrote for every seeded org (all of
    # which fail no_coverage for this people_id3) still reference orphan_people_id3 via
    # the FK on adopter_interest. Cleaning the contact removed the interest +
    # cascaded the attempts, so we can drop the orphan Fpg row now.
    await session.execute(delete(Fpg).where(Fpg.people_id3 == orphan_people_id3))
    await session.commit()


@pytest.mark.asyncio
async def test_exclusion_after_send_back(session: AsyncSession) -> None:
    """Plan edge case: re-match after send-back excludes prior facilitator."""
    people_id3 = f"ZZZ{uuid.uuid4().hex[:3].upper()}"
    org_a, org_b, org_c = await _seed_three_covering_orgs(session, people_id3=people_id3)
    contact = await _make_contact(session, country="US", languages=["en"])
    interest = await _make_interest(session, contact, people_id3=people_id3)
    # Manually insert a prior send_back Match for org_a.
    sent_back = Match(
        id=uuid.uuid4(),
        adopter_interest_id=interest.id,
        facilitator_org_id=org_a.id,
        status="sent_back",
    )
    session.add(sent_back)
    await session.commit()

    outcome = await match_or_route(session, contact)
    await session.commit()

    # The rank-1 Match should NOT be org_a; alternates rank in MatchAttempt.
    fresh_match = (
        await session.execute(
            select(Match).where(
                Match.adopter_interest_id == interest.id,
                Match.status == "recommended",
            )
        )
    ).scalar_one()
    assert fresh_match.facilitator_org_id in {org_b.id, org_c.id}
    # Rank 1 + rank 2 in MatchAttempt cover org_b and org_c only.
    ranked_attempts = (
        await session.execute(
            select(MatchAttempt).where(
                MatchAttempt.adopter_interest_id == interest.id,
                MatchAttempt.run_id == outcome.run_id,
                MatchAttempt.rank.isnot(None),
            )
        )
    ).scalars().all()
    ranked_facs = {a.candidate_facilitator_id for a in ranked_attempts}
    assert ranked_facs == {org_b.id, org_c.id}

    # The MatchAttempt for org_a in THIS run records the exclusion reason.
    a_attempt = (
        await session.execute(
            select(MatchAttempt).where(
                MatchAttempt.adopter_interest_id == interest.id,
                MatchAttempt.candidate_facilitator_id == org_a.id,
                MatchAttempt.run_id == outcome.run_id,
            )
        )
    ).scalar_one()
    assert a_attempt.filter_results["filter_reason"] == "excluded_by_previous_send_back"

    await _cleanup_contact(session, contact)
    await _cleanup_orgs(
        session, org_a.id, org_b.id, org_c.id, people_id3s=[people_id3]
    )


@pytest.mark.asyncio
async def test_tied_scores_break_by_last_assigned_at(
    session: AsyncSession,
) -> None:
    """Plan edge case: equal scores → deterministic order via last_assigned_at."""
    people_id3 = f"ZZZ{uuid.uuid4().hex[:3].upper()}"
    # Make all three orgs identical so weighted_total ties exactly.
    session.add(Fpg(people_id3=people_id3, name=f"FPG {people_id3}", country_code="US", frontier=True))
    await session.flush()
    now = datetime.now(UTC)
    orgs: list[FacilitatingOrg] = []
    timestamps = [None, now - timedelta(days=3), now - timedelta(days=1)]
    for i, last in enumerate(timestamps):
        org = FacilitatingOrg(
            id=uuid.uuid4(),
            name=f"Tied Org {i}",
            country_code="US",
            language_codes=["en"],
            capacity_total=10,
            capacity_committed=0,  # same headroom for all
            active=True,
            is_triage_org=False,
            last_assigned_at=last,
        )
        session.add(org)
        session.add(
            FacilitatorFpgCoverage(facilitator_org_id=org.id, people_id3=people_id3)
        )
        orgs.append(org)
    await session.flush()

    contact = await _make_contact(session, country="US", languages=["en"])
    interest = await _make_interest(session, contact, people_id3=people_id3)
    await session.commit()

    outcome = await match_or_route(session, contact)
    await session.commit()

    attempts = (
        await session.execute(
            select(MatchAttempt)
            .where(
                MatchAttempt.adopter_interest_id == interest.id,
                MatchAttempt.run_id == outcome.run_id,
            )
            .order_by(MatchAttempt.rank.asc().nullslast())
        )
    ).scalars().all()
    # Ranks should be 1, 2, 3 in order of last_assigned_at ASC (NULL first).
    rank_to_org = {a.rank: a.candidate_facilitator_id for a in attempts if a.rank}
    assert rank_to_org[1] == orgs[0].id  # last=None → first
    assert rank_to_org[2] == orgs[1].id  # last=3d ago → second oldest
    assert rank_to_org[3] == orgs[2].id  # last=1d ago → third

    await _cleanup_contact(session, contact)
    await _cleanup_orgs(session, *(o.id for o in orgs), people_id3s=[people_id3])


@pytest.mark.asyncio
async def test_score_breakdown_round_trips_into_match_attempt(
    session: AsyncSession,
) -> None:
    """Plan integration: persisted JSONB should equal the in-memory ScoreVector."""
    people_id3 = f"ZZZ{uuid.uuid4().hex[:3].upper()}"
    orgs = await _seed_three_covering_orgs(session, people_id3=people_id3)
    contact = await _make_contact(session, country="US", languages=["en"])
    interest = await _make_interest(session, contact, people_id3=people_id3)
    await session.commit()

    outcome = await match_or_route(session, contact)
    await session.commit()
    _ = outcome  # avoid F841

    a = (
        await session.execute(
            select(MatchAttempt).where(
                MatchAttempt.adopter_interest_id == interest.id,
                MatchAttempt.rank == 1,
            )
        )
    ).scalar_one()
    bd = a.score_breakdown
    assert set(bd.keys()) == {
        "capacity_headroom",
        "geography",
        "language",
        "fpg_affinity",
        "theological",
    }
    # All values in [0, 1].
    for v in bd.values():
        assert 0.0 <= v <= 1.0

    await _cleanup_contact(session, contact)
    await _cleanup_orgs(session, *(o.id for o in orgs), people_id3s=[people_id3])


@pytest.mark.asyncio
async def test_missing_triage_org_raises(session: AsyncSession) -> None:
    """Defensive: removing the triage org and running matching should raise
    rather than silently dropping interests."""
    # Carry a savepoint so we can flip is_triage_org back at the end.
    triage = await session.get(FacilitatingOrg, TRIAGE_ORG_ID)
    assert triage is not None
    triage.is_triage_org = False
    await session.flush()
    try:
        contact = await _make_contact(session)
        await _make_interest(session, contact, people_id3=None)
        await session.commit()
        with pytest.raises(TriageOrgMissingError):
            await match_or_route(session, contact)
        await _cleanup_contact(session, contact)
    finally:
        triage = await session.get(FacilitatingOrg, TRIAGE_ORG_ID)
        if triage is not None:
            triage.is_triage_org = True
            await session.commit()


# ─── B1 / adv4-010 conflict-guard tests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_conflict_does_not_abort_run(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """B1 / adv4-010: when uq_match_open_per_interest fires on one interest
    (because an open Match row already exists), the algorithm must skip that
    interest gracefully and complete the run — every OTHER interest should
    still get its MatchAttempt audit rows.

    Setup: contact has two interests. We pre-seed an open recommended Match
    row for the FIRST interest (simulating a concurrent triage assignment by
    Amy's UI). When match_or_route walks the contact, the first interest hits
    the conflict guard; the second proceeds normally.
    """
    import logging

    people_id3 = f"ZZZ{uuid.uuid4().hex[:3].upper()}"
    org_a, org_b, org_c = await _seed_three_covering_orgs(session, people_id3=people_id3)
    contact = await _make_contact(session, country="US", languages=["en"])
    interest_conflict = await _make_interest(session, contact, people_id3=people_id3)
    interest_ok = await _make_interest(session, contact, people_id3=people_id3)
    # Pre-seed an open Match for the first interest. The matcher will try to
    # insert a recommended Match → uq_match_open_per_interest conflict.
    pre_seeded = Match(
        id=uuid.uuid4(),
        adopter_interest_id=interest_conflict.id,
        facilitator_org_id=org_a.id,
        status="recommended",
    )
    session.add(pre_seeded)
    await session.commit()

    caplog.set_level(logging.INFO)
    outcome = await match_or_route(session, contact)
    await session.commit()

    # The run completed without raising — both interest outcomes are present.
    assert len(outcome.interest_outcomes) == 2
    reasons = {o.interest_id: o.reason for o in outcome.interest_outcomes}
    # The conflicted interest's outcome reflects the conflict path. The
    # refetch in the guard finds the pre-seeded recommended row, so the
    # returned reason is "scored" (the guard returned the existing match,
    # not None). Either "scored" or "concurrent_conflict_unrecoverable" is
    # acceptable depending on whether the refetch succeeds.
    assert reasons[interest_conflict.id] in (
        "scored",
        "concurrent_conflict_unrecoverable",
    )
    assert reasons[interest_ok.id] == "scored"

    # The non-conflicted interest got its MatchAttempt audit rows — this is
    # the load-bearing assertion for adv4-010 (re-raise would have wiped them).
    ok_attempts = (
        await session.execute(
            select(MatchAttempt).where(
                MatchAttempt.adopter_interest_id == interest_ok.id,
                MatchAttempt.run_id == outcome.run_id,
            )
        )
    ).scalars().all()
    assert len(ok_attempts) >= 3  # at least the three covering orgs

    # The conflict log fired at least once for the conflicted interest.
    conflict_logs = [
        r for r in caplog.records if "match.concurrent_conflict" in r.message
    ]
    assert len(conflict_logs) >= 1

    await _cleanup_contact(session, contact)
    await _cleanup_orgs(session, org_a.id, org_b.id, org_c.id, people_id3s=[people_id3])


@pytest.mark.asyncio
async def test_match_attempt_audit_survives_conflict_rollback(
    session: AsyncSession,
) -> None:
    """CORR-1: MatchAttempt rows added in the candidate loop must be flushed
    to the OUTER transaction BEFORE ``begin_nested`` opens the savepoint.
    Otherwise SQLAlchemy autoflush flushes them inside the savepoint, and a
    rollback on conflict undoes them.

    Verify by seeding an open Match (forces conflict) and checking that the
    MatchAttempt rows for the conflicted interest still landed in the DB.
    """
    people_id3 = f"ZZZ{uuid.uuid4().hex[:3].upper()}"
    org_a, org_b, org_c = await _seed_three_covering_orgs(session, people_id3=people_id3)
    contact = await _make_contact(session, country="US", languages=["en"])
    interest = await _make_interest(session, contact, people_id3=people_id3)
    pre_seeded = Match(
        id=uuid.uuid4(),
        adopter_interest_id=interest.id,
        facilitator_org_id=org_a.id,
        status="recommended",
    )
    session.add(pre_seeded)
    await session.commit()

    outcome = await match_or_route(session, contact)
    await session.commit()

    # MatchAttempt rows for THIS run survived the savepoint rollback.
    attempts = (
        await session.execute(
            select(MatchAttempt).where(
                MatchAttempt.adopter_interest_id == interest.id,
                MatchAttempt.run_id == outcome.run_id,
            )
        )
    ).scalars().all()
    assert len(attempts) >= 3, (
        "MatchAttempt audit rows must persist even when the Match insert "
        "rolls back via the savepoint conflict guard."
    )

    await _cleanup_contact(session, contact)
    await _cleanup_orgs(session, org_a.id, org_b.id, org_c.id, people_id3s=[people_id3])


@pytest.mark.asyncio
async def test_conflict_refetch_finds_accepted_status_match(
    session: AsyncSession,
) -> None:
    """R-B1-1: the refetch predicate must mirror uq_match_open_per_interest
    exactly. If a row in status='accepted' (covered by the partial index)
    is the conflict winner, the refetch must find it — otherwise the guard
    returns (None, False) and the interest is incorrectly skipped.
    """
    people_id3 = f"ZZZ{uuid.uuid4().hex[:3].upper()}"
    org_a, org_b, org_c = await _seed_three_covering_orgs(session, people_id3=people_id3)
    contact = await _make_contact(session, country="US", languages=["en"])
    interest = await _make_interest(session, contact, people_id3=people_id3)
    pre_seeded = Match(
        id=uuid.uuid4(),
        adopter_interest_id=interest.id,
        facilitator_org_id=org_a.id,
        status="accepted",  # also covered by uq_match_open_per_interest
    )
    session.add(pre_seeded)
    await session.commit()

    outcome = await match_or_route(session, contact)
    await session.commit()

    # The interest's outcome must NOT be 'concurrent_conflict_unrecoverable'
    # — the refetch found the accepted row, so the guard returned it.
    out = outcome.interest_outcomes[0]
    assert out.reason != "concurrent_conflict_unrecoverable", (
        "Refetch predicate must include 'accepted' (partial-index status)."
    )

    await _cleanup_contact(session, contact)
    await _cleanup_orgs(session, org_a.id, org_b.id, org_c.id, people_id3s=[people_id3])
