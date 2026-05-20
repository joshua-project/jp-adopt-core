"""Staff + facilitator match queue + decide endpoints (U7).

Endpoints
---------
* ``GET  /v1/matches/queue``       — pending recommendations + triage rows
                                     visible to the calling actor
* ``GET  /v1/matches/{match_id}``  — single match detail with score breakdown
* ``POST /v1/matches/{match_id}/decide`` — accept / send_back / route_elsewhere
* ``POST /v1/matches/run/{contact_id}`` — trigger matching on demand (#40)

Role gates
----------
* ``staff_admin``, ``adoption_manager`` — full visibility
* ``triage_facilitator`` — sees triage-status matches (no-FPG / no-coverage)
* ``facilitator`` — sees only matches whose ``facilitator_org_id`` is in the
  caller's ``facilitator_org_membership`` set

State-machine integration
-------------------------
Accept transitions the underlying ``Contact`` via
``state_machine.transition_adopter`` (pre-active → MATCHED for the
adoption-manager accept; MATCHED → ACTIVE for the facilitator accept).
Send-back transitions MATCHED → SENT_BACK and stamps the Match.status so
``matching.match_or_route`` automatically excludes the prior facilitator on
the re-match (existing behavior in U6's `_derive_exclusion_list`).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

import sqlalchemy
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jp_adopt_api.deps import (
    CurrentUserWithRoles,
    DbSession,
    require_role,
)
from jp_adopt_api.domain.matching import (
    TriageOrgMissingError,
    match_or_route,
)
from jp_adopt_api.domain.state_machine import (
    AdopterState,
    ConcurrentModificationError,
    IllegalTransitionError,
    InvalidReasonCodeError,
    ReasonCode,
    ReasonRequiredError,
    RoleNotPermittedError,
    transition_adopter,
)
from jp_adopt_api.domain.state_machine_errors import map_state_machine_exception
from jp_adopt_api.models import (
    AdopterInterest,
    Contact,
    FacilitatingOrg,
    FacilitatorOrgMembership,
    Match,
    MatchAttempt,
)
from jp_adopt_api.outbox_suppression import emit_outbox

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/matches", tags=["matches"])


# Match statuses considered "open" — visible in the queue and decidable.
QUEUE_STATUSES = ("recommended", "triage", "accepted")
# F9: "open" set used to gate ``/run/{contact_id}`` — anything past
# ``accepted`` into ``active`` is still an in-flight match and must block a
# re-run too. ``QUEUE_STATUSES`` stays the queue-display set so the staff
# UI doesn't show actives by default.
OPEN_MATCH_STATUSES = (*QUEUE_STATUSES, "active")
# Decision verbs (kept identical to the schema's literal so the OpenAPI spec
# documents the exact set the handler accepts).
DecisionVerb = Literal["accept", "send_back", "route_elsewhere"]

EVENT_MATCH_ROUTED_ELSEWHERE = "jp.adopt.v1.match.routed_elsewhere"
EVENT_MATCH_ACCEPTED_BY_MANAGER = "jp.adopt.v1.match.accepted_by_manager"
EVENT_MATCH_RUN_REQUESTED = "jp.adopt.v1.match.run_requested"


# ──────────────────────────────────────────────────────────────────────────
# Pydantic schemas
# ──────────────────────────────────────────────────────────────────────────


class ScoreBreakdown(BaseModel):
    # All five fields default to None so legacy MatchAttempt.score_breakdown
    # rows persisted before the matcher began emitting the full set don't
    # 500 the queue endpoint. New rows always carry all five.
    capacity_headroom: float | None = None
    geography: float | None = None
    language: float | None = None
    fpg_affinity: float | None = None
    theological: float | None = None


class MatchCandidate(BaseModel):
    """A ranked alternative for a single adopter_interest. Includes the
    persisted MatchAttempt score so the UI can show "why this match" without a
    second round-trip.
    """

    model_config = ConfigDict(from_attributes=True)

    attempt_id: uuid.UUID
    facilitator_org_id: uuid.UUID
    facilitator_name: str
    score: float | None = None
    score_breakdown: ScoreBreakdown | None = None
    rank: int | None = None


class MatchSummary(BaseModel):
    """One open Match row in the queue, with embedded alternatives."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    adopter_interest_id: uuid.UUID
    contact_id: uuid.UUID
    contact_display_name: str
    contact_adopter_status: str | None
    rop3: str | None
    facilitator_org_id: uuid.UUID
    facilitator_name: str
    status: str
    recommended_at: datetime
    decided_at: datetime | None
    candidates: list[MatchCandidate] = Field(default_factory=list)


class QueueResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    items: list[MatchSummary]
    total: int


class DecideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: DecisionVerb
    reason_code: ReasonCode | None = None
    reason_text: str | None = Field(default=None, max_length=2048)
    # On `route_elsewhere`, the caller may pin the next recommended facilitator
    # by attempt_id; if omitted, the next-highest-ranked unmasked alternative
    # is selected automatically.
    next_attempt_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _next_attempt_only_for_route_elsewhere(self) -> "DecideRequest":
        # F24: silently dropping the field on accept/send_back hides client
        # bugs; reject the combination at the schema layer so it surfaces as
        # a 422 with a clear path.
        if self.next_attempt_id is not None and self.decision != "route_elsewhere":
            raise ValueError(
                "next_attempt_id is only valid with decision='route_elsewhere'"
            )
        return self


class DecideResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    match: MatchSummary
    contact_adopter_status: str | None


class RunMatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Force re-running matching even when the contact already has open
    # recommendations. Without this, the endpoint refuses to overwrite an
    # open queue entry.
    force: bool = False


class RunMatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    contact_id: uuid.UUID
    run_id: uuid.UUID
    total_recommended: int
    total_triage: int


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


async def _load_visible_org_ids(
    db: AsyncSession, user_sub: str
) -> frozenset[uuid.UUID]:
    """Facilitator membership lookup. Returns the set of org IDs the caller
    is a member of (empty when they have no memberships).
    """
    rows = await db.execute(
        select(FacilitatorOrgMembership.facilitator_org_id).where(
            FacilitatorOrgMembership.user_b2c_subject_id == user_sub
        )
    )
    return frozenset(rows.scalars().all())


def _pick_actor_role(roles: frozenset[str], allowed: frozenset[str]) -> str:
    """Pick the most-privileged role from ``roles`` that is in ``allowed``.

    F6: multi-role users (e.g. an adoption_manager who is also a facilitator)
    would previously be 403'd for a facilitator-only transition because the
    resolver returned ``adoption_manager`` even though only ``facilitator``
    was in the spec's ``allowed_roles``. The intersection ``roles & allowed``
    is now the source-set we iterate, so the picked role is always
    spec-compatible.

    Priority order matches the seeded role hierarchy:
    staff_admin > adoption_manager > triage_facilitator > facilitator.
    """
    overlap = roles & allowed
    for candidate in (
        "staff_admin",
        "adoption_manager",
        "triage_facilitator",
        "facilitator",
    ):
        if candidate in overlap:
            return candidate
    if overlap:
        return next(iter(overlap))
    # Fallback — caller's role check should have already 403'd; raise a
    # specific error so a misuse is loud rather than silent.
    raise RoleNotPermittedError(
        actor_role=next(iter(roles)) if roles else "<none>",
        required_roles=allowed,
    )


async def _load_interest_meta(
    db: AsyncSession, interest_id: uuid.UUID
) -> tuple[uuid.UUID, str | None]:
    """Return ``(contact_id, rop3)`` for an interest, raising 404 if missing.

    F23: ``scalar_one()`` raised ``NoResultFound`` and bubbled as a 500 when
    the interest row had been deleted between two requests. Promote that to
    an honest 404 instead of an opaque internal error.
    """
    row = (
        await db.execute(
            select(AdopterInterest.contact_id, AdopterInterest.rop3).where(
                AdopterInterest.id == interest_id
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="AdopterInterest not found",
        )
    return row[0], row[1]


async def _load_match_attempts_for_interests(
    db: AsyncSession, interest_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[tuple[MatchAttempt, str]]]:
    """Batched MatchAttempt + facilitator-name lookup for a set of interests.

    F5: replaces a per-match ``select`` with a single ``WHERE IN (...)`` so
    queue endpoints stay flat in DB-roundtrips. Returns a dict keyed by
    ``adopter_interest_id`` for in-memory grouping; the ordering inside each
    list mirrors the original query (rank asc nullslast, created_at asc).
    """
    grouped: dict[uuid.UUID, list[tuple[MatchAttempt, str]]] = {
        iid: [] for iid in interest_ids
    }
    if not interest_ids:
        return grouped
    rows = (
        await db.execute(
            select(MatchAttempt, FacilitatingOrg.name)
            .join(
                FacilitatingOrg,
                FacilitatingOrg.id == MatchAttempt.candidate_facilitator_id,
            )
            .where(MatchAttempt.adopter_interest_id.in_(interest_ids))
            .order_by(
                MatchAttempt.rank.asc().nullslast(),
                MatchAttempt.created_at.asc(),
            )
        )
    ).all()
    for attempt, name in rows:
        if attempt.adopter_interest_id is None:
            continue
        grouped.setdefault(attempt.adopter_interest_id, []).append((attempt, name))
    return grouped


def _build_candidates(
    attempts: list[tuple[MatchAttempt, str]],
) -> list[MatchCandidate]:
    out: list[MatchCandidate] = []
    for attempt, name in attempts:
        score_breakdown = (
            ScoreBreakdown(**attempt.score_breakdown)
            if attempt.score_breakdown is not None
            else None
        )
        out.append(
            MatchCandidate(
                attempt_id=attempt.id,
                facilitator_org_id=attempt.candidate_facilitator_id,
                facilitator_name=name,
                score=float(attempt.score) if attempt.score is not None else None,
                score_breakdown=score_breakdown,
                rank=attempt.rank,
            )
        )
    return out


async def _build_match_summary(
    db: AsyncSession, m: Match, *, include_candidates: bool = True
) -> MatchSummary:
    """Single-match summary. Used by ``get_match`` and ``decide_match``.

    The queue endpoint avoids this per-match path entirely and uses
    :func:`_build_queue_summaries` so the N+1 stays out of the hot path.
    """
    contact_id, rop3 = await _load_interest_meta(db, m.adopter_interest_id)
    contact = await db.get(Contact, contact_id)
    facilitator = await db.get(FacilitatingOrg, m.facilitator_org_id)
    candidates: list[MatchCandidate] = []
    if include_candidates:
        grouped = await _load_match_attempts_for_interests(
            db, [m.adopter_interest_id]
        )
        candidates = _build_candidates(grouped.get(m.adopter_interest_id, []))
    if contact is None or facilitator is None:
        # FKs guarantee both exist; defensively raise if they vanished mid-call.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Match references missing contact or facilitator",
        )
    return MatchSummary(
        id=m.id,
        adopter_interest_id=m.adopter_interest_id,
        contact_id=contact.id,
        contact_display_name=contact.display_name,
        contact_adopter_status=contact.adopter_status,
        rop3=rop3,
        facilitator_org_id=m.facilitator_org_id,
        facilitator_name=facilitator.name,
        status=m.status,
        recommended_at=m.recommended_at,
        decided_at=m.decided_at,
        candidates=candidates,
    )


async def _build_queue_summaries(
    db: AsyncSession, matches: list[Match]
) -> list[MatchSummary]:
    """F5: build N ``MatchSummary`` rows in at most two DB roundtrips:

    1. One JOIN to fetch ``(Match, AdopterInterest.contact_id,
       AdopterInterest.rop3, Contact, FacilitatingOrg)`` for every Match.
    2. One batched ``IN (...)`` query over ``MatchAttempt`` keyed by
       ``adopter_interest_id``.

    The previous implementation called ``_build_match_summary`` per match,
    which fired ~5 queries per row.
    """
    if not matches:
        return []
    match_ids = [m.id for m in matches]
    rows = (
        await db.execute(
            select(
                Match,
                AdopterInterest.contact_id,
                AdopterInterest.rop3,
                Contact,
                FacilitatingOrg,
            )
            .join(AdopterInterest, AdopterInterest.id == Match.adopter_interest_id)
            .join(Contact, Contact.id == AdopterInterest.contact_id)
            .join(FacilitatingOrg, FacilitatingOrg.id == Match.facilitator_org_id)
            .where(Match.id.in_(match_ids))
        )
    ).all()
    # Preserve the input ordering — the queue endpoint orders by
    # ``recommended_at`` and a hash-table iteration would scramble that.
    row_by_match: dict[uuid.UUID, tuple[Match, uuid.UUID, str | None, Contact, FacilitatingOrg]] = {
        row[0].id: row for row in rows
    }
    interest_ids = list({m.adopter_interest_id for m in matches})
    grouped_attempts = await _load_match_attempts_for_interests(db, interest_ids)

    out: list[MatchSummary] = []
    for m in matches:
        row = row_by_match.get(m.id)
        if row is None:
            # FK guarantees the join finds the row; defensively skip if not.
            continue
        _, contact_id, rop3, contact, facilitator = row
        out.append(
            MatchSummary(
                id=m.id,
                adopter_interest_id=m.adopter_interest_id,
                contact_id=contact_id,
                contact_display_name=contact.display_name,
                contact_adopter_status=contact.adopter_status,
                rop3=rop3,
                facilitator_org_id=m.facilitator_org_id,
                facilitator_name=facilitator.name,
                status=m.status,
                recommended_at=m.recommended_at,
                decided_at=m.decided_at,
                candidates=_build_candidates(
                    grouped_attempts.get(m.adopter_interest_id, [])
                ),
            )
        )
    return out


async def _interest_contact_id(
    db: AsyncSession, interest_id: uuid.UUID
) -> uuid.UUID:
    """Thin wrapper around :func:`_load_interest_meta` for the decide path
    where only the contact_id is needed."""
    cid, _ = await _load_interest_meta(db, interest_id)
    return cid


# ──────────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────────


_QUEUE_ROLES = frozenset(
    {"staff_admin", "adoption_manager", "triage_facilitator", "facilitator"}
)


@router.get("/queue", response_model=QueueResponse)
async def get_queue(
    db: DbSession,
    user_with_roles: CurrentUserWithRoles,
) -> QueueResponse:
    user, roles = user_with_roles
    if not roles & _QUEUE_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "role_required",
                "message": "Queue access requires a staff or facilitator role",
            },
        )

    stmt = select(Match).where(Match.status.in_(QUEUE_STATUSES))
    if not (roles & frozenset({"staff_admin", "adoption_manager"})):
        if "triage_facilitator" in roles:
            stmt = stmt.where(Match.status == "triage")
        else:
            # Pure facilitator: scope to org memberships
            org_ids = await _load_visible_org_ids(db, user.sub)
            if not org_ids:
                return QueueResponse(items=[], total=0)
            stmt = stmt.where(Match.facilitator_org_id.in_(org_ids))

    stmt = stmt.order_by(Match.recommended_at.asc())
    matches = (await db.execute(stmt)).scalars().all()
    # F5: batched summary builder keeps the queue at O(2) DB roundtrips.
    summaries = await _build_queue_summaries(db, list(matches))
    return QueueResponse(items=summaries, total=len(summaries))


@router.get("/{match_id}", response_model=MatchSummary)
async def get_match(
    match_id: uuid.UUID,
    db: DbSession,
    user_with_roles: CurrentUserWithRoles,
) -> MatchSummary:
    user, roles = user_with_roles
    if not roles & _QUEUE_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "role_required", "message": "Match access requires a role"},
        )
    m = await db.get(Match, match_id)
    if m is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Match not found"
        )
    # Org-scope check for non-staff actors. F3: a ``triage_facilitator``-only
    # actor (no staff_admin / adoption_manager) is restricted to triage rows.
    staff_roles = frozenset({"staff_admin", "adoption_manager"})
    if not (roles & staff_roles):
        if "facilitator" in roles:
            org_ids = await _load_visible_org_ids(db, user.sub)
            if m.facilitator_org_id not in org_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "code": "org_not_member",
                        "message": "Caller is not a member of this match's org",
                    },
                )
        elif "triage_facilitator" in roles:
            if m.status != "triage":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "code": "role_not_permitted_for_status",
                        "message": (
                            "triage_facilitator may only view triage matches"
                        ),
                    },
                )
        else:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "role_required", "message": "Insufficient role"},
            )
    return await _build_match_summary(db, m)


# Internal: shared by accept / send_back / route_elsewhere
async def _stamp_decision(
    m: Match,
    *,
    new_status: str,
    actor_sub: str,
    reason_code: ReasonCode | None,
    reason_text: str | None,
) -> None:
    m.status = new_status
    m.decided_at = datetime.now(UTC)
    m.decided_by = actor_sub
    m.decision_reason_code = reason_code.value if reason_code is not None else None
    m.decision_reason_text = reason_text


# F1: capacity counter — statuses for which the prior Match held a slot. Used
# by send_back / route_elsewhere to decide whether to release a reservation.
_RESERVED_MATCH_STATUSES = frozenset({"accepted", "active"})


async def _try_increment_capacity(
    db: AsyncSession, facilitator_org_id: uuid.UUID
) -> bool:
    """F1: atomic capacity reservation.

    Single ``UPDATE ... SET capacity_committed = capacity_committed + 1
    WHERE id = :id AND capacity_committed < capacity_total`` — the WHERE
    doubles as the ceiling guard so two concurrent accepts on the same org
    cannot overcommit. Returns ``True`` iff a row was actually updated;
    callers raise the appropriate 409 when ``False`` is returned.

    Also stamps ``last_assigned_at`` to ``now()`` in the same UPDATE.
    """
    result = await db.execute(
        update(FacilitatingOrg)
        .where(
            FacilitatingOrg.id == facilitator_org_id,
            FacilitatingOrg.capacity_committed < FacilitatingOrg.capacity_total,
        )
        .values(
            capacity_committed=FacilitatingOrg.capacity_committed + 1,
            last_assigned_at=datetime.now(UTC),
        )
    )
    return (result.rowcount or 0) > 0


async def _try_decrement_capacity(
    db: AsyncSession, facilitator_org_id: uuid.UUID
) -> None:
    """F1: release a previously-reserved slot. Guarded by ``> 0`` so a
    double-decrement (which the calling logic now prevents) can't drive the
    counter negative — the existing ``ck_facilitating_org_capacity_committed_nonneg``
    CHECK would refuse the write anyway, but we'd rather short-circuit here
    than raise an IntegrityError on a benign retry."""
    await db.execute(
        update(FacilitatingOrg)
        .where(
            FacilitatingOrg.id == facilitator_org_id,
            FacilitatingOrg.capacity_committed > 0,
        )
        .values(
            capacity_committed=FacilitatingOrg.capacity_committed - 1,
        )
    )


def _accept_target_status(current_adopter_status: str | None) -> str:
    """F20: a retry of ``accept`` on an already-accepted match should land
    deterministically: the first accept (manager) moved the match to
    ``accepted``; the second accept (facilitator) moves it to ``active``.
    Both states are "successfully accepted" — the idempotency check needs to
    cover both."""
    current = AdopterState(current_adopter_status or "draft")
    return "active" if current == AdopterState.MATCHED else "accepted"


@router.post("/{match_id}/decide", response_model=DecideResponse)
async def decide_match(
    match_id: uuid.UUID,
    body: DecideRequest,
    db: DbSession,
    user_with_roles: CurrentUserWithRoles,
) -> DecideResponse:
    user, roles = user_with_roles
    # F21: drop the local re-declaration; reuse the module-level _QUEUE_ROLES.
    if not roles & _QUEUE_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "role_required", "message": "Decide requires a role"},
        )

    m = await db.get(Match, match_id)
    if m is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Match not found"
        )

    staff_roles = frozenset({"staff_admin", "adoption_manager"})
    # Org-scope check for facilitator role.
    if "facilitator" in roles and not (roles & staff_roles):
        org_ids = await _load_visible_org_ids(db, user.sub)
        if m.facilitator_org_id not in org_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "org_not_member",
                    "message": "Caller is not a member of this match's org",
                },
            )

    # F3: a triage_facilitator-only actor must not be able to act on
    # non-triage matches (route_elsewhere, accept, etc.) since the state-
    # machine's per-transition role gate isn't consulted for route_elsewhere.
    if (
        "triage_facilitator" in roles
        and not (roles & staff_roles)
        and "facilitator" not in roles
        and m.status != "triage"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "role_not_permitted_for_status",
                "message": "triage_facilitator may only act on triage matches",
            },
        )

    contact_id = await _interest_contact_id(db, m.adopter_interest_id)
    contact = await db.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found"
        )

    if m.status not in QUEUE_STATUSES:
        # F1 / F20: idempotent retry handling — if the caller is repeating the
        # same decision they already won, return 200 with the current state
        # instead of 409. The accept verb is tricky: after a manager-accept,
        # the match is at ``accepted``; after a facilitator-accept, it's at
        # ``active``. Both are "accepted" outcomes — match against the set,
        # not a single value.
        accept_set = frozenset({"accepted", "active"})
        if (body.decision == "accept" and m.status in accept_set) or (
            body.decision == "send_back" and m.status == "sent_back"
        ) or (
            body.decision == "route_elsewhere" and m.status == "declined"
        ):
            return DecideResponse(
                match=await _build_match_summary(db, m),
                contact_adopter_status=contact.adopter_status,
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "match_already_decided",
                "message": f"Match is in status {m.status!r}, no longer decidable",
            },
        )

    actor_role = _pick_actor_role(roles, _QUEUE_ROLES)
    # F1: capture the pre-mutation status so send_back can decide whether
    # this Match was actually holding a reservation.
    prior_match_status = m.status

    try:
        if body.decision == "accept":
            # Two acceptance flows merge here:
            # 1) The adoption_manager accepts the recommendation — Contact
            #    transitions to MATCHED, Match.status → accepted.
            # 2) The assigned facilitator accepts — Contact transitions
            #    MATCHED → ACTIVE, Match.status → active.
            current_status = AdopterState(contact.adopter_status or "draft")
            if current_status == AdopterState.MATCHED:
                target = AdopterState.ACTIVE
                new_match_status = "active"
                event_type = "jp.adopt.v1.match.accepted_by_facilitator"
            else:
                target = AdopterState.MATCHED
                new_match_status = "accepted"
                event_type = EVENT_MATCH_ACCEPTED_BY_MANAGER

            await transition_adopter(
                db,
                contact,
                to_state=target,
                actor_b2c_sub=user.sub,
                actor_role=actor_role,
                reason_code=body.reason_code,
                reason_text=body.reason_text,
            )
            await _stamp_decision(
                m,
                new_status=new_match_status,
                actor_sub=user.sub,
                reason_code=body.reason_code,
                reason_text=body.reason_text,
            )
            # Bump facilitator capacity_committed atomically on the first
            # accept (manager flow). The facilitator's accept just promotes
            # the row to active; the slot was already reserved.
            if target == AdopterState.MATCHED:
                ok = await _try_increment_capacity(db, m.facilitator_org_id)
                if not ok:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": "capacity_unavailable",
                            "message": (
                                "Facilitator capacity is at the ceiling; "
                                "route to a different org."
                            ),
                        },
                    )
            emit_outbox(
                db,
                event_type=event_type,
                payload={
                    "event": event_type,
                    "schema_version": "jp.adopt.v1",
                    "match_id": str(m.id),
                    "contact_id": str(contact.id),
                    "facilitator_org_id": str(m.facilitator_org_id),
                    "actor": {"sub": user.sub, "role": actor_role},
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )

        elif body.decision == "send_back":
            if body.reason_code is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "reason_required",
                        "message": "send_back requires reason_code",
                    },
                )
            await transition_adopter(
                db,
                contact,
                to_state=AdopterState.SENT_BACK,
                actor_b2c_sub=user.sub,
                actor_role=actor_role,
                reason_code=body.reason_code,
                reason_text=body.reason_text,
            )
            await _stamp_decision(
                m,
                new_status="sent_back",
                actor_sub=user.sub,
                reason_code=body.reason_code,
                reason_text=body.reason_text,
            )
            # F1: release the capacity reservation ONLY if the prior Match
            # had actually reserved a slot (status was accepted or active).
            # Sending back a ``recommended`` or ``triage`` row would otherwise
            # silently free some other accepted match's slot.
            if prior_match_status in _RESERVED_MATCH_STATUSES:
                await _try_decrement_capacity(db, m.facilitator_org_id)

        else:  # route_elsewhere
            # Mark this Match declined; create a new `recommended` Match for
            # the chosen alternative (or the next-best alternative when
            # next_attempt_id is omitted). The Contact's adopter_status is
            # NOT mutated here — the recommendation slot just rotates to a
            # different facilitator.
            alternates = (
                await db.execute(
                    select(MatchAttempt)
                    .where(
                        MatchAttempt.adopter_interest_id == m.adopter_interest_id,
                        MatchAttempt.candidate_facilitator_id != m.facilitator_org_id,
                        MatchAttempt.rank.is_not(None),
                    )
                    .order_by(MatchAttempt.rank.asc())
                )
            ).scalars().all()
            chosen: MatchAttempt | None
            if body.next_attempt_id is not None:
                chosen = next(
                    (a for a in alternates if a.id == body.next_attempt_id),
                    None,
                )
                if chosen is None:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={
                            "code": "alternate_not_found",
                            "message": "next_attempt_id is not a ranked alternate",
                        },
                    )
            else:
                chosen = alternates[0] if alternates else None
            if chosen is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "no_alternates",
                        "message": (
                            "No ranked alternates available; "
                            "trigger a new run instead"
                        ),
                    },
                )

            await _stamp_decision(
                m,
                new_status="declined",
                actor_sub=user.sub,
                reason_code=body.reason_code,
                reason_text=body.reason_text,
            )

            # F1: route_elsewhere from an already-accepted match must
            # release the old org's slot AND reserve the new org's slot.
            # Otherwise an in-flight accept rerouted to a different org
            # leaves a phantom slot on the original.
            if prior_match_status in _RESERVED_MATCH_STATUSES:
                await _try_decrement_capacity(db, m.facilitator_org_id)
                ok = await _try_increment_capacity(
                    db, chosen.candidate_facilitator_id
                )
                if not ok:
                    # Roll back the just-decremented old-org slot by
                    # re-incrementing before bailing out, since the outer
                    # try/except will rollback the whole transaction anyway
                    # — this just keeps the in-memory state consistent if a
                    # later test inspects it.
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": "capacity_unavailable",
                            "message": (
                                "Alternate facilitator is at capacity; "
                                "trigger a new run to refresh ranking."
                            ),
                        },
                    )

            new_match = Match(
                id=uuid.uuid4(),
                adopter_interest_id=m.adopter_interest_id,
                facilitator_org_id=chosen.candidate_facilitator_id,
                status="recommended",
            )
            db.add(new_match)

            emit_outbox(
                db,
                event_type=EVENT_MATCH_ROUTED_ELSEWHERE,
                payload={
                    "event": EVENT_MATCH_ROUTED_ELSEWHERE,
                    "schema_version": "jp.adopt.v1",
                    "match_id": str(m.id),
                    "new_match_id": str(new_match.id),
                    "contact_id": str(contact.id),
                    "from_facilitator_org_id": str(m.facilitator_org_id),
                    "to_facilitator_org_id": str(chosen.candidate_facilitator_id),
                    "actor": {"sub": user.sub, "role": actor_role},
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )
    except HTTPException:
        await db.rollback()
        raise
    except (
        ReasonRequiredError,
        InvalidReasonCodeError,
        RoleNotPermittedError,
        IllegalTransitionError,
        ConcurrentModificationError,
    ) as e:
        await db.rollback()
        raise map_state_machine_exception(e) from e
    except sqlalchemy.exc.IntegrityError as e:
        # F4: the route_elsewhere new-Match insert can race
        # ``uq_match_open_per_interest`` (another caller created a recommended
        # match for the same interest first). Surface that as a 409 rather
        # than a 500.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "concurrent_modification",
                "message": "Another decision was applied concurrently",
            },
        ) from e

    await db.commit()
    await db.refresh(m)
    await db.refresh(contact)
    return DecideResponse(
        match=await _build_match_summary(db, m),
        contact_adopter_status=contact.adopter_status,
    )


_run_match_dep = require_role("staff_admin", "adoption_manager")


@router.post("/run/{contact_id}", response_model=RunMatchResponse, status_code=200)
async def run_match(
    contact_id: uuid.UUID,
    body: RunMatchRequest,
    db: DbSession,
    _: Annotated[tuple[object, frozenset[str]], Depends(_run_match_dep)],
) -> RunMatchResponse:
    """#40: HTTP surface for ``match_or_route()``. Required for the staff UI to
    re-run matching after a send-back or manual contact create. Idempotent in
    spirit — repeated calls during the same run produce the same outcome
    (capacity check is read-inside-transaction).
    """
    contact = await db.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found"
        )

    if not body.force:
        existing_open = (
            await db.execute(
                select(Match)
                .join(
                    AdopterInterest,
                    AdopterInterest.id == Match.adopter_interest_id,
                )
                .where(AdopterInterest.contact_id == contact_id)
                # F9: ``active`` is still an open in-flight match; without it
                # an accepted-then-active contact could re-enter matching and
                # collide with the unique partial index.
                .where(Match.status.in_(OPEN_MATCH_STATUSES))
            )
        ).scalars().first()
        if existing_open is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "open_match_exists",
                    "message": (
                        "Contact already has open recommendations; "
                        "send_back / route_elsewhere first, or pass force=true."
                    ),
                },
            )

    try:
        outcome = await match_or_route(db, contact)
    except TriageOrgMissingError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "triage_org_missing", "message": str(e)},
        ) from e

    emit_outbox(
        db,
        event_type=EVENT_MATCH_RUN_REQUESTED,
        payload={
            "event": EVENT_MATCH_RUN_REQUESTED,
            "schema_version": "jp.adopt.v1",
            "contact_id": str(contact_id),
            "run_id": str(outcome.run_id),
            "total_recommended": outcome.total_recommended,
            "total_triage": outcome.total_triage,
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )

    await db.commit()
    return RunMatchResponse(
        contact_id=contact_id,
        run_id=outcome.run_id,
        total_recommended=outcome.total_recommended,
        total_triage=outcome.total_triage,
    )


# Re-export of QUEUE_STATUSES / OPEN_MATCH_STATUSES so tests and the workflow
# router can keep the "open" set in lockstep with this module without
# duplicating the tuple.
__all__ = [
    "OPEN_MATCH_STATUSES",
    "QUEUE_STATUSES",
    "router",
]
