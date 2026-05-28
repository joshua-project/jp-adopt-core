"""Matching algorithm + triage routing (U6).

Public entrypoint: ``match_or_route(session, contact, *, run_id=None,
exclude_facilitator_ids=None)``.

For each ``AdopterInterest`` row attached to the contact:

* If the interest has no FPG selected (`people_id3 IS NULL`), insert one Match row
  pointing at the configured triage org with `status='triage'`. No
  MatchAttempt rows are written for the no-FPG case (there's nothing to
  rank). The plan calls for a single triage assignment per no-FPG interest.

* Otherwise, build the candidate set: all facilitating_org rows where
  ``hard_filter`` returns True. Score every candidate, persist one
  MatchAttempt row per candidate (with score breakdown + filter results,
  rank 1..N for ranked candidates). Insert **one** Match row with
  `status='recommended'` pointing at the rank-1 candidate; the alternates
  are visible through the MatchAttempt rows (Amy's UI in U7 reads them to
  power the "route-elsewhere" choice).

  This deviates from the plan's "top-3 to Match rows" wording in order to
  satisfy U5's `uq_match_open_per_interest` partial index, which enforces
  "at most one open Match per interest" across recommended/accepted/active/
  triage statuses. The wording's intent ("Amy sees three ranked options"
  on the queue UI) is preserved by ranking alternates in MatchAttempt;
  documented in ``docs/runbooks/matching-algorithm-v1.md``.

* If the hard filter yields zero candidates (the "no_coverage" case), assign
  the interest to the triage queue as well. This deviates from the plan's
  strict "0 Match rows" wording in favor of one consistent UI surface — the
  triage queue — that captures every interest needing human routing
  regardless of why it ended up there. The audit trail distinguishes the
  cases: no-FPG interests have `people_id3 IS NULL`; no-coverage interests have a
  concrete `people_id3` plus zero MatchAttempt rows. Documented in
  ``docs/runbooks/matching-algorithm-v1.md``.

The function flushes but does NOT commit. The caller controls the
transaction boundary, matching the state-machine module's pattern.

Exclusion list semantics (re-match after send-back):
* If ``exclude_facilitator_ids`` is supplied, those org IDs are removed from
  the candidate pool before scoring.
* When ``exclude_facilitator_ids is None``, the function derives the
  exclusion list automatically from prior Match rows for the contact in
  status ``sent_back`` or ``declined``. Pass an explicit empty set to
  override (e.g., test scenarios that want to score against everyone
  including past send-backs).
"""

from __future__ import annotations

import enum
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jp_adopt_api.domain.matching_config import (
    CAPACITY_DIVIDE_BY_ZERO_FALLBACK,
    DEFAULT_WEIGHTS,
    MIN_SCORE_FLOOR,
    TOP_N_RECOMMENDED,
    MatchingWeights,
)
from jp_adopt_api.models import (
    AdopterInterest,
    Contact,
    FacilitatingOrg,
    FacilitatorFpgCoverage,
    Match,
    MatchAttempt,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Errors
# ──────────────────────────────────────────────────────────────────────────


class MatchingError(Exception):
    """Base class for matching algorithm errors."""


class TriageOrgMissingError(MatchingError):
    """Raised when no facilitating_org has is_triage_org=TRUE and an interest
    needs triage routing. Configuration error — the U5 seed inserts one
    triage org, but production must keep one alive (the unique partial index
    on `is_triage_org` enforces 'at most one' but not 'at least one')."""


# ──────────────────────────────────────────────────────────────────────────
# Data types
# ──────────────────────────────────────────────────────────────────────────


class FilterReason(enum.StrEnum):
    """Why a facilitator was kept or rejected during hard filtering. Used in
    the persisted ``match_attempt.filter_results`` JSONB for auditability."""

    PASSED = "passed"
    INACTIVE = "inactive"
    NO_CAPACITY = "no_capacity"
    NO_COVERAGE = "no_coverage"
    EXCLUDED = "excluded_by_previous_send_back"


@dataclass(frozen=True)
class ScoreVector:
    """Per-signal scores for one (contact_interest, facilitator) pairing.

    All component values are bounded in [0, 1]. The weighted sum is in
    [0, 1] when weights sum to 1.0 (enforced by ``matching_config``).
    """

    capacity_headroom: float
    geography: float
    language: float
    fpg_affinity: float
    theological: float

    def weighted_total(self, w: MatchingWeights = DEFAULT_WEIGHTS) -> float:
        return (
            self.capacity_headroom * w.capacity_headroom
            + self.geography * w.geography
            + self.language * w.language
            + self.fpg_affinity * w.fpg_affinity
            + self.theological * w.theological
        )

    def as_dict(self) -> dict[str, float]:
        # Kept as a plain dict because the JSONB column stores it verbatim.
        return {
            "capacity_headroom": self.capacity_headroom,
            "geography": self.geography,
            "language": self.language,
            "fpg_affinity": self.fpg_affinity,
            "theological": self.theological,
        }


@dataclass
class Candidate:
    facilitator: FacilitatingOrg
    score_vector: ScoreVector | None = None
    filter_reason: FilterReason = FilterReason.PASSED
    # Set of people_id3s this facilitator covers — cached on the candidate so each
    # interest's hard_filter / score doesn't re-query the coverage table.
    covered_people_id3s: frozenset[str] = field(default_factory=frozenset)

    @property
    def passed_filter(self) -> bool:
        return self.filter_reason == FilterReason.PASSED


@dataclass
class MatchOutcome:
    """Returned by ``match_or_route`` so callers can inspect / log without
    re-querying the DB. The persistence happened inside the function; this is
    purely a structured summary."""

    contact_id: uuid.UUID
    run_id: uuid.UUID
    interest_outcomes: list[InterestOutcome] = field(default_factory=list)

    @property
    def total_recommended(self) -> int:
        return sum(len(io.recommended_match_ids) for io in self.interest_outcomes)

    @property
    def total_triage(self) -> int:
        return sum(1 for io in self.interest_outcomes if io.triage_match_id is not None)


@dataclass
class InterestOutcome:
    interest_id: uuid.UUID
    people_id3: str | None
    triage_match_id: uuid.UUID | None = None
    recommended_match_ids: list[uuid.UUID] = field(default_factory=list)
    attempt_ids: list[uuid.UUID] = field(default_factory=list)
    reason: str | None = None  # e.g. "no_fpg", "no_coverage", "scored"


# ──────────────────────────────────────────────────────────────────────────
# Hard filter
# ──────────────────────────────────────────────────────────────────────────


def hard_filter(
    *,
    facilitator: FacilitatingOrg,
    people_id3: str,
    covered_people_id3s: frozenset[str],
    excluded_facilitator_ids: frozenset[uuid.UUID],
) -> FilterReason:
    """Return PASSED if this facilitator is a candidate for this people_id3, else
    a specific FilterReason describing what bounced them. Pure function —
    has no DB access; the caller pre-loads coverage.

    N6: F36 added a ``contact_has_no_fpg`` parameter that gated on
    ``facilitator.accepting_potential_adopters``. Reverted because the
    production caller in ``_process_interest`` short-circuits to triage
    when ``people_id3 IS None`` BEFORE this filter ever runs, and the only call
    site passed ``contact_has_no_fpg=False`` unconditionally — making
    the branch unreachable. The ``accepting_potential_adopters`` column
    on ``FacilitatingOrg`` is intentionally retained: it will be wired
    in once ``match_or_route`` gains an explicit no-FPG branch that
    selects alternative triage orgs (planned for U7+).
    """
    if facilitator.id in excluded_facilitator_ids:
        return FilterReason.EXCLUDED
    if not facilitator.active:
        return FilterReason.INACTIVE
    # capacity_committed < capacity_total is the gate. capacity_total of 0
    # means "we accept zero adopters right now" so it fails closed.
    if facilitator.capacity_committed >= facilitator.capacity_total:
        return FilterReason.NO_CAPACITY
    if people_id3 not in covered_people_id3s:
        return FilterReason.NO_COVERAGE
    return FilterReason.PASSED


# ──────────────────────────────────────────────────────────────────────────
# Score components
# ──────────────────────────────────────────────────────────────────────────


def _capacity_headroom_score(facilitator: FacilitatingOrg) -> float:
    """(capacity_total - capacity_committed) / capacity_total, clamped [0, 1].

    Larger free capacity → higher score. Zero-capacity facilitators are
    filtered out by ``hard_filter`` already, but the divide-by-zero fallback
    keeps the function pure (callable independent of the filter result)."""
    total = facilitator.capacity_total or CAPACITY_DIVIDE_BY_ZERO_FALLBACK
    free = max(facilitator.capacity_total - facilitator.capacity_committed, 0)
    return min(free / total, 1.0)


def _geography_score(contact: Contact, facilitator: FacilitatingOrg) -> float:
    """1.0 on exact country match, 0.0 otherwise. v1 has no region/continent
    logic — that's a v2 refinement once Amy has data on whether
    cross-country matches actually work."""
    if not contact.country_code or not facilitator.country_code:
        return 0.0
    return 1.0 if contact.country_code == facilitator.country_code else 0.0


def _language_score(contact: Contact, facilitator: FacilitatingOrg) -> float:
    """|intersection| / |union| of language sets. Both empty → 0.0."""
    if not contact.language_codes or not facilitator.language_codes:
        return 0.0
    contact_langs = {c.lower() for c in contact.language_codes}
    fac_langs = {c.lower() for c in facilitator.language_codes}
    union = contact_langs | fac_langs
    if not union:
        return 0.0
    return len(contact_langs & fac_langs) / len(union)


def _fpg_affinity_score(
    people_id3: str, covered_people_id3s: frozenset[str]
) -> float:
    """1.0 if this people_id3 is in the facilitator's coverage set (else 0).

    Single-FPG scoring is binary in v1 — the multi-FPG case is handled at
    the loop level by scoring each interest separately. A v2 refinement
    could weight by how many *other* contact interests this facilitator
    also covers (i.e., one-stop-shop bonus)."""
    return 1.0 if people_id3 in covered_people_id3s else 0.0


def _theological_score(contact: Contact, facilitator: FacilitatingOrg) -> float:
    """Contact doesn't have a theological_tags field in v1; this score is
    always 0 for now. Kept in the signature so the weight slot exists and
    the runbook can document the design space. v2 wires in adopter
    preferences from a contact extension table."""
    return 0.0


def score(
    *,
    contact: Contact,
    facilitator: FacilitatingOrg,
    people_id3: str,
    covered_people_id3s: frozenset[str],
) -> ScoreVector:
    """Compute the full score vector for one (contact, facilitator, people_id3)
    pairing. Caller multiplies by weights via ``ScoreVector.weighted_total``."""
    return ScoreVector(
        capacity_headroom=_capacity_headroom_score(facilitator),
        geography=_geography_score(contact, facilitator),
        language=_language_score(contact, facilitator),
        fpg_affinity=_fpg_affinity_score(people_id3, covered_people_id3s),
        theological=_theological_score(contact, facilitator),
    )


# ──────────────────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────────────────


async def _load_facilitators_with_coverage(
    session: AsyncSession,
) -> list[Candidate]:
    """Single query for all facilitating orgs + their coverage. We then run
    hard_filter / score in Python rather than push the scoring into SQL —
    it's a small set (orgs are O(dozens) in v1) and the audit trail is much
    easier to capture from Python objects."""
    orgs = (
        await session.execute(
            select(FacilitatingOrg).where(
                FacilitatingOrg.is_triage_org.is_(False),
            )
        )
    ).scalars().all()
    if not orgs:
        return []
    coverage_rows = (
        await session.execute(
            select(
                FacilitatorFpgCoverage.facilitator_org_id,
                FacilitatorFpgCoverage.people_id3,
            )
        )
    ).all()
    coverage: dict[uuid.UUID, set[str]] = {}
    for org_id, people_id3 in coverage_rows:
        coverage.setdefault(org_id, set()).add(people_id3)
    return [
        Candidate(
            facilitator=org,
            covered_people_id3s=frozenset(coverage.get(org.id, set())),
        )
        for org in orgs
    ]


async def _resolve_triage_org(session: AsyncSession) -> FacilitatingOrg:
    triage = (
        await session.execute(
            select(FacilitatingOrg).where(FacilitatingOrg.is_triage_org.is_(True))
        )
    ).scalar_one_or_none()
    if triage is None:
        raise TriageOrgMissingError(
            "No facilitating_org has is_triage_org=TRUE. The U5 seed inserts "
            "one ('Triage Queue'); restore it or seed a fresh one before "
            "running matching."
        )
    return triage


async def _derive_exclusion_list(
    session: AsyncSession,
    *,
    contact_id: uuid.UUID,
) -> frozenset[uuid.UUID]:
    """Facilitators that previously sent this contact back or declined them."""
    interest_ids = (
        await session.execute(
            select(AdopterInterest.id).where(
                AdopterInterest.contact_id == contact_id
            )
        )
    ).scalars().all()
    if not interest_ids:
        return frozenset()
    excluded = (
        await session.execute(
            select(Match.facilitator_org_id).where(
                and_(
                    Match.adopter_interest_id.in_(interest_ids),
                    Match.status.in_(("sent_back", "declined")),
                )
            )
        )
    ).scalars().all()
    return frozenset(excluded)


# ──────────────────────────────────────────────────────────────────────────
# Per-interest processing
# ──────────────────────────────────────────────────────────────────────────


def _sort_candidates_for_ranking(
    scored: Iterable[tuple[Candidate, float]],
) -> list[tuple[Candidate, float]]:
    """Sort highest weighted_total first; tiebreak on oldest last_assigned_at
    (None = never assigned, which sorts first via the (None, id) coercion
    below). Deterministic — no random ordering anywhere."""

    def sort_key(item: tuple[Candidate, float]) -> tuple[float, float, str]:
        cand, total = item
        # Negate the score so reverse-ascending sort puts highest first.
        # last_assigned_at: None → -inf so unassigned facilitators bubble up.
        last = cand.facilitator.last_assigned_at
        last_ts = last.timestamp() if last is not None else float("-inf")
        return (-total, last_ts, str(cand.facilitator.id))

    return sorted(scored, key=sort_key)


def _outbox_payload_for_attempt(
    *,
    candidate: Candidate,
    people_id3: str,
    score_vector: ScoreVector | None,
    weighted_total: float | None,
) -> dict[str, Any]:
    """Shape of the per-attempt audit row's filter_results JSONB."""
    return {
        "facilitator_org_id": str(candidate.facilitator.id),
        "facilitator_name": candidate.facilitator.name,
        "people_id3": people_id3,
        "filter_reason": candidate.filter_reason.value,
        "facilitator_active": candidate.facilitator.active,
        "facilitator_capacity_committed": candidate.facilitator.capacity_committed,
        "facilitator_capacity_total": candidate.facilitator.capacity_total,
        "covered_people_id3s": sorted(candidate.covered_people_id3s),
        "weighted_total": weighted_total,
        # score_breakdown is also stored separately in match_attempt.score_breakdown;
        # repeated here so filter_results is self-contained for log inspection.
        "score_breakdown": score_vector.as_dict() if score_vector is not None else None,
    }


# The set of Match statuses covered by the ``uq_match_open_per_interest``
# partial unique index. Keep in lockstep with ``OPEN_MATCH_STATUSES`` in
# alembic/versions/20260517_0005_match_domain.py — both must list the same
# statuses or the refetch in ``_insert_match_with_conflict_guard`` will miss
# the winner's row and the conflict guard will incorrectly re-raise / skip.
_OPEN_MATCH_STATUSES_FOR_CONFLICT_REFETCH = (
    "recommended",
    "accepted",
    "active",
    "triage",
)


async def _insert_match_with_conflict_guard(
    session: AsyncSession,
    *,
    match: Match,
    interest_id: uuid.UUID,
) -> tuple[Match | None, bool]:
    """Insert a Match row, tolerating the ``uq_match_open_per_interest`` race.

    B1: a concurrent triage assignment (e.g. Amy's UI promoting a routed
    match while the matcher is mid-run) can race the unique partial index
    and surface as ``IntegrityError`` here. Without a guard, the entire
    ``match_or_route`` call aborts and the MatchAttempt audit rows for
    all subsequent interests are lost.

    On conflict, rollback to the savepoint and refetch the existing open
    Match for this interest. Returns ``(match, created)``:
      * ``(match, True)``  — insert succeeded.
      * ``(existing_match, False)`` — winner's row found on refetch.
      * ``(None, False)`` — refetch returned nothing (the winner's status
        flipped off the open-statuses set between the conflict and the
        refetch). Caller treats this as an unrecoverable per-interest
        skip and continues the run (R-adv4-010).

    R-B1-1: the refetch's ``status IN (...)`` predicate must mirror
    ``uq_match_open_per_interest``'s partial index exactly. See
    ``_OPEN_MATCH_STATUSES_FOR_CONFLICT_REFETCH`` above.
    """
    try:
        async with session.begin_nested():
            session.add(match)
            await session.flush()
        return match, True
    except IntegrityError:
        # The savepoint has been rolled back automatically by the nested
        # transaction context manager — we can still query and continue.
        existing = (
            await session.execute(
                select(Match)
                .where(Match.adopter_interest_id == interest_id)
                .where(
                    Match.status.in_(_OPEN_MATCH_STATUSES_FOR_CONFLICT_REFETCH)
                )
            )
        ).scalar_one_or_none()
        logger.info(
            "match.concurrent_conflict interest=%s existing_match=%s",
            interest_id,
            existing.id if existing else None,
        )
        return existing, False


async def _process_interest(
    session: AsyncSession,
    *,
    contact: Contact,
    interest: AdopterInterest,
    candidates: list[Candidate],
    triage_org: FacilitatingOrg,
    excluded: frozenset[uuid.UUID],
    weights: MatchingWeights,
    run_id: uuid.UUID,
) -> InterestOutcome:
    outcome = InterestOutcome(
        interest_id=interest.id, people_id3=interest.people_id3
    )

    # --- No-FPG path ---------------------------------------------------
    if interest.people_id3 is None:
        # CORR-1: flush ANY pending objects to the outer transaction
        # BEFORE opening the savepoint. Autoflush inside ``begin_nested``
        # would flush them within the savepoint, so a savepoint rollback
        # on conflict would undo unrelated audit inserts too. (No-FPG
        # path has no audit rows yet, but mirror the pattern for safety
        # if ``_process_interest`` ever grows a pre-savepoint mutation.)
        await session.flush()
        match = Match(
            id=uuid.uuid4(),
            adopter_interest_id=interest.id,
            facilitator_org_id=triage_org.id,
            status="triage",
        )
        match_row, _created = await _insert_match_with_conflict_guard(
            session, match=match, interest_id=interest.id
        )
        if match_row is None:
            # adv4-010: conflict path, winner's row gone by refetch → skip
            # this interest rather than aborting the whole run.
            logger.warning(
                "match.concurrent_conflict_unrecoverable interest=%s contact=%s",
                interest.id, contact.id,
            )
            outcome.reason = "concurrent_conflict_unrecoverable"
            return outcome
        outcome.triage_match_id = match_row.id
        outcome.reason = "no_fpg"
        logger.info(
            "matching: no_fpg interest=%s contact=%s → triage match=%s",
            interest.id, contact.id, match_row.id,
        )
        return outcome

    people_id3 = interest.people_id3

    # --- Score every candidate -----------------------------------------
    scored: list[tuple[Candidate, float]] = []
    # F9: track the MatchAttempt rows we create for this interest in a local
    # list (no longer scanning ``session.new`` later) so rank backfill is
    # immune to unrelated flushes or sibling iterations adding/removing rows.
    attempts_for_this_interest: list[MatchAttempt] = []
    for cand_template in candidates:
        # Each interest needs its own Candidate so filter_reason / score
        # don't leak across interests within the run.
        cand = Candidate(
            facilitator=cand_template.facilitator,
            covered_people_id3s=cand_template.covered_people_id3s,
            filter_reason=hard_filter(
                facilitator=cand_template.facilitator,
                people_id3=people_id3,
                covered_people_id3s=cand_template.covered_people_id3s,
                excluded_facilitator_ids=excluded,
            ),
        )
        if cand.passed_filter:
            cand.score_vector = score(
                contact=contact,
                facilitator=cand.facilitator,
                people_id3=people_id3,
                covered_people_id3s=cand.covered_people_id3s,
            )
            # F43: round once at the source. Both the rank ordering AND the
            # persisted ``score`` column read this same value, so floating-
            # point drift between "weighted for sort" and "weighted for
            # storage" can't desynchronize them.
            weighted = round(cand.score_vector.weighted_total(weights), 3)
            scored.append((cand, weighted))
        else:
            weighted = None  # type: ignore[assignment]
        # Persist a MatchAttempt for *every* candidate considered (pass or
        # fail) so the audit can answer "why didn't org X match?" without
        # rerunning the algorithm.
        # F31: reuse the local ``weighted`` value here instead of recomputing
        # ``cand.score_vector.weighted_total(weights)`` two more times.
        attempt = MatchAttempt(
            id=uuid.uuid4(),
            contact_id=contact.id,
            adopter_interest_id=interest.id,
            run_id=run_id,
            candidate_facilitator_id=cand.facilitator.id,
            score=(Decimal(str(weighted)) if weighted is not None else None),
            score_breakdown=cand.score_vector.as_dict()
            if cand.score_vector is not None
            else None,
            filter_results=_outbox_payload_for_attempt(
                candidate=cand,
                people_id3=people_id3,
                score_vector=cand.score_vector,
                weighted_total=weighted,
            ),
            rank=None,  # set below for promoted candidates
        )
        session.add(attempt)
        attempts_for_this_interest.append(attempt)
        outcome.attempt_ids.append(attempt.id)

    # Hard-filter shut out everyone → triage queue.
    if not scored:
        # CORR-1: flush the MatchAttempt rows added in the candidate loop
        # to the OUTER transaction before opening the savepoint. Otherwise
        # SQLAlchemy autoflush would flush them inside ``begin_nested``,
        # and a savepoint rollback on conflict would undo every audit row
        # we just spent the loop computing.
        await session.flush()
        match = Match(
            id=uuid.uuid4(),
            adopter_interest_id=interest.id,
            facilitator_org_id=triage_org.id,
            status="triage",
        )
        match_row, _created = await _insert_match_with_conflict_guard(
            session, match=match, interest_id=interest.id
        )
        if match_row is None:
            logger.warning(
                "match.concurrent_conflict_unrecoverable interest=%s "
                "contact=%s people_id3=%s",
                interest.id, contact.id, people_id3,
            )
            outcome.reason = "concurrent_conflict_unrecoverable"
            return outcome
        outcome.triage_match_id = match_row.id
        outcome.reason = "no_coverage"
        logger.info(
            "matching: no_coverage interest=%s contact=%s people_id3=%s → triage match=%s",
            interest.id, contact.id, people_id3, match_row.id,
        )
        return outcome

    # --- Rank + promote top-N ------------------------------------------
    ranked = _sort_candidates_for_ranking(scored)
    promoted = [
        (cand, total)
        for cand, total in ranked
        if total >= MIN_SCORE_FLOOR
    ][:TOP_N_RECOMMENDED]
    # Backfill rank on the promoted MatchAttempt rows. We iterate the
    # ``attempts_for_this_interest`` list captured during scoring rather than
    # walking ``session.new`` — see F9: the session.new walk caught the
    # right rows in practice but only because nothing else was happening
    # in the session. Any concurrent mutation (sibling interest iteration,
    # an outer caller adding rows) could shift which rows the loop ranked.
    promoted_attempt_index: dict[uuid.UUID, int] = {}
    for idx, (cand, _total) in enumerate(promoted, start=1):
        promoted_attempt_index[cand.facilitator.id] = idx
    for attempt_obj in attempts_for_this_interest:
        rank = promoted_attempt_index.get(attempt_obj.candidate_facilitator_id)
        if rank is not None:
            attempt_obj.rank = rank

    # Only ONE recommended Match row per interest — uq_match_open_per_interest
    # forbids more. The rank-1 candidate becomes that row; alternates live in
    # MatchAttempt with rank 2/3 for U7's "route-elsewhere" UI.
    if promoted:
        top_cand, _top_total = promoted[0]
        # CORR-1: flush MatchAttempt audit rows to the outer transaction
        # BEFORE the savepoint opens, so a conflict rollback does not also
        # undo the audit inserts we just spent the candidate loop computing.
        await session.flush()
        match = Match(
            id=uuid.uuid4(),
            adopter_interest_id=interest.id,
            facilitator_org_id=top_cand.facilitator.id,
            status="recommended",
        )
        match_row, _created = await _insert_match_with_conflict_guard(
            session, match=match, interest_id=interest.id
        )
        if match_row is None:
            logger.warning(
                "match.concurrent_conflict_unrecoverable interest=%s "
                "contact=%s people_id3=%s",
                interest.id, contact.id, people_id3,
            )
            outcome.reason = "concurrent_conflict_unrecoverable"
            return outcome
        outcome.recommended_match_ids.append(match_row.id)
    else:
        await session.flush()
    outcome.reason = "scored"
    logger.info(
        "matching: scored interest=%s contact=%s people_id3=%s candidates=%d "
        "ranked=%d promoted_top=%s top_score=%.3f",
        interest.id, contact.id, people_id3, len(scored), len(promoted),
        promoted[0][0].facilitator.id if promoted else None,
        ranked[0][1] if ranked else 0.0,
    )
    return outcome


# ──────────────────────────────────────────────────────────────────────────
# Public entrypoint
# ──────────────────────────────────────────────────────────────────────────


async def match_or_route(
    session: AsyncSession,
    contact: Contact,
    *,
    run_id: uuid.UUID | None = None,
    exclude_facilitator_ids: frozenset[uuid.UUID] | None = None,
    weights: MatchingWeights = DEFAULT_WEIGHTS,
) -> MatchOutcome:
    """Run the matching algorithm for one contact.

    For every AdopterInterest attached to the contact, either route to the
    triage queue (no FPG / no coverage / hard-filter-empty) or rank + promote
    candidates. Persists Match + MatchAttempt rows; caller commits.
    """
    if run_id is None:
        run_id = uuid.uuid4()

    interests = (
        await session.execute(
            select(AdopterInterest).where(AdopterInterest.contact_id == contact.id)
        )
    ).scalars().all()
    outcome = MatchOutcome(contact_id=contact.id, run_id=run_id)
    if not interests:
        logger.warning(
            "matching: contact=%s has no adopter_interest rows; nothing to do",
            contact.id,
        )
        return outcome

    candidates = await _load_facilitators_with_coverage(session)
    triage_org = await _resolve_triage_org(session)
    if exclude_facilitator_ids is None:
        exclude_facilitator_ids = await _derive_exclusion_list(
            session, contact_id=contact.id
        )

    for interest in interests:
        result = await _process_interest(
            session,
            contact=contact,
            interest=interest,
            candidates=candidates,
            triage_org=triage_org,
            excluded=exclude_facilitator_ids,
            weights=weights,
            run_id=run_id,
        )
        outcome.interest_outcomes.append(result)

    return outcome


__all__ = [
    "Candidate",
    "FilterReason",
    "InterestOutcome",
    "MatchOutcome",
    "MatchingError",
    "ScoreVector",
    "TriageOrgMissingError",
    "hard_filter",
    "match_or_route",
    "score",
]
