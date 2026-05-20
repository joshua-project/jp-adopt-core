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

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
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
    capacity_headroom: float
    geography: float
    language: float
    fpg_affinity: float
    theological: float


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
    Priority order matches the seeded role hierarchy:
    staff_admin > adoption_manager > triage_facilitator > facilitator.
    """
    for candidate in (
        "staff_admin",
        "adoption_manager",
        "triage_facilitator",
        "facilitator",
    ):
        if candidate in roles and candidate in allowed:
            return candidate
    # No matching role: pick any role in ``allowed`` that the user holds.
    overlap = roles & allowed
    if overlap:
        return next(iter(overlap))
    # Fallback — caller's role check should have already 403'd; raise a
    # specific error so a misuse is loud rather than silent.
    raise RoleNotPermittedError(
        actor_role=next(iter(roles)) if roles else "<none>",
        required_roles=allowed,
    )


async def _build_match_summary(
    db: AsyncSession, m: Match, *, include_candidates: bool = True
) -> MatchSummary:
    contact_id = await _interest_contact_id(db, m.adopter_interest_id)
    contact = await db.get(Contact, contact_id)
    facilitator = await db.get(FacilitatingOrg, m.facilitator_org_id)
    rop3 = await _interest_rop3(db, m.adopter_interest_id)
    candidates: list[MatchCandidate] = []
    if include_candidates:
        attempts = (
            await db.execute(
                select(MatchAttempt, FacilitatingOrg.name)
                .join(
                    FacilitatingOrg,
                    FacilitatingOrg.id == MatchAttempt.candidate_facilitator_id,
                )
                .where(MatchAttempt.adopter_interest_id == m.adopter_interest_id)
                .order_by(
                    MatchAttempt.rank.asc().nullslast(),
                    MatchAttempt.created_at.asc(),
                )
            )
        ).all()
        for attempt, name in attempts:
            score_breakdown = (
                ScoreBreakdown(**attempt.score_breakdown)
                if attempt.score_breakdown is not None
                else None
            )
            candidates.append(
                MatchCandidate(
                    attempt_id=attempt.id,
                    facilitator_org_id=attempt.candidate_facilitator_id,
                    facilitator_name=name,
                    score=float(attempt.score) if attempt.score is not None else None,
                    score_breakdown=score_breakdown,
                    rank=attempt.rank,
                )
            )
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


async def _interest_contact_id(
    db: AsyncSession, interest_id: uuid.UUID
) -> uuid.UUID:
    cid = (
        await db.execute(
            select(AdopterInterest.contact_id).where(AdopterInterest.id == interest_id)
        )
    ).scalar_one()
    return cid


async def _interest_rop3(db: AsyncSession, interest_id: uuid.UUID) -> str | None:
    return (
        await db.execute(
            select(AdopterInterest.rop3).where(AdopterInterest.id == interest_id)
        )
    ).scalar_one()


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
    summaries = [await _build_match_summary(db, m) for m in matches]
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
    # Org-scope check for non-staff actors.
    if not (roles & frozenset({"staff_admin", "adoption_manager"})):
        if "triage_facilitator" in roles and m.status == "triage":
            pass  # ok
        elif "facilitator" in roles:
            org_ids = await _load_visible_org_ids(db, user.sub)
            if m.facilitator_org_id not in org_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "code": "org_not_member",
                        "message": "Caller is not a member of this match's org",
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


def _map_state_machine_exception(e: Exception) -> HTTPException:
    if isinstance(e, ReasonRequiredError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "reason_required", "message": str(e)},
        )
    if isinstance(e, InvalidReasonCodeError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_reason_code", "message": str(e)},
        )
    if isinstance(e, RoleNotPermittedError):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "role_not_permitted", "message": str(e)},
        )
    if isinstance(e, IllegalTransitionError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "illegal_transition", "message": str(e)},
        )
    if isinstance(e, ConcurrentModificationError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "concurrent_modification", "message": str(e)},
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"code": "internal_error", "message": "Unexpected transition error"},
    )


@router.post("/{match_id}/decide", response_model=DecideResponse)
async def decide_match(
    match_id: uuid.UUID,
    body: DecideRequest,
    db: DbSession,
    user_with_roles: CurrentUserWithRoles,
) -> DecideResponse:
    user, roles = user_with_roles
    decide_roles = frozenset(
        {"staff_admin", "adoption_manager", "triage_facilitator", "facilitator"}
    )
    if not roles & decide_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "role_required", "message": "Decide requires a role"},
        )

    m = await db.get(Match, match_id)
    if m is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Match not found"
        )

    # Org-scope check for facilitator role.
    if "facilitator" in roles and not (
        roles & frozenset({"staff_admin", "adoption_manager"})
    ):
        org_ids = await _load_visible_org_ids(db, user.sub)
        if m.facilitator_org_id not in org_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "org_not_member",
                    "message": "Caller is not a member of this match's org",
                },
            )

    if m.status not in QUEUE_STATUSES:
        # F1: idempotent retry handling — if the caller is repeating the same
        # decision they already won, return 200 with the current state instead
        # of 409. A fresh, different decision on an already-decided match is
        # 409 (conflict).
        target_status = {
            "accept": "accepted",
            "send_back": "sent_back",
            "route_elsewhere": "declined",
        }[body.decision]
        if m.status == target_status:
            return DecideResponse(
                match=await _build_match_summary(db, m),
                contact_adopter_status=(
                    await db.get(
                        Contact,
                        await _interest_contact_id(db, m.adopter_interest_id),
                    )
                ).adopter_status,
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "match_already_decided",
                "message": f"Match is in status {m.status!r}, no longer decidable",
            },
        )

    contact_id = await _interest_contact_id(db, m.adopter_interest_id)
    contact = await db.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found"
        )

    actor_role = _pick_actor_role(roles, decide_roles)

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
            # Bump facilitator capacity_committed on first accept; the
            # facilitator's "accept" only counts when transitioning to active,
            # but the manager's accept reserves the slot so the row is
            # excluded from further runs.
            if target == AdopterState.MATCHED:
                facilitator = await db.get(FacilitatingOrg, m.facilitator_org_id)
                if facilitator is not None:
                    facilitator.capacity_committed += 1
                    facilitator.last_assigned_at = datetime.now(UTC)
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
            # Release the capacity reservation: a sent-back match no longer
            # holds the slot. Only decrement if we actually counted it (>0).
            facilitator = await db.get(FacilitatingOrg, m.facilitator_org_id)
            if facilitator is not None and facilitator.capacity_committed > 0:
                facilitator.capacity_committed -= 1

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
        raise _map_state_machine_exception(e) from e

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
                .where(Match.status.in_(QUEUE_STATUSES))
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


# Re-export of QUEUE_STATUSES so tests and the workflow router can keep the
# "open" set in lockstep with this module without duplicating the tuple.
__all__ = [
    "QUEUE_STATUSES",
    "router",
]
