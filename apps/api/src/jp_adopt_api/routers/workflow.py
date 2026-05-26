"""Adoption / facilitator processing workflow router (U8).

Endpoint
--------
* ``POST /v1/contacts/{contact_id}/transition`` — generic state-machine
  entrypoint over HTTP. Used by Amy's workflow view (transition any contact
  by status) and as a server-side surface for transitions that don't fit
  neatly into the match-queue verbs (e.g. ``new → potential_adopter`` triage,
  ``matched → active`` facilitator acceptance, ``any → do_not_engage`` block).

The ``/v1/matches/{id}/decide`` endpoint (U7) remains the canonical surface
for accept / send_back / route_elsewhere; this router is for the long tail.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from jp_adopt_api.deps import (
    CurrentUserWithRoles,
    DbSession,
    require_role,
)
from jp_adopt_api.domain.state_machine import (
    AdopterState,
    ConcurrentModificationError,
    FacilitatorState,
    IllegalTransitionError,
    InvalidReasonCodeError,
    ReasonCode,
    ReasonRequiredError,
    RoleNotPermittedError,
    transition_adopter,
    transition_facilitator,
)
from jp_adopt_api.domain.state_machine_errors import map_state_machine_exception
from jp_adopt_api.models import (
    AdopterInterest,
    Contact,
    FacilitatorOrgMembership,
    Match,
)
from jp_adopt_api.routers.matches import OPEN_MATCH_STATUSES
from jp_adopt_api.schemas import ContactRead

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/contacts", tags=["workflow"])


_WORKFLOW_ROLES = frozenset(
    {"staff_admin", "adoption_manager", "triage_facilitator", "facilitator"}
)


class TransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["adopter", "facilitator"]
    to_state: str = Field(min_length=1, max_length=64)
    reason_code: ReasonCode | None = None
    reason_text: str | None = Field(default=None, max_length=2048)


class TransitionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    contact: ContactRead
    transitioned_to: str


_workflow_dep = require_role(*_WORKFLOW_ROLES)


def _pick_actor_role(roles: frozenset[str]) -> str:
    for candidate in (
        "staff_admin",
        "adoption_manager",
        "triage_facilitator",
        "facilitator",
    ):
        if candidate in roles:
            return candidate
    # Defensive: ``_workflow_dep`` would have already 403'd if no role
    # overlapped, so this branch is unreachable at runtime.
    return next(iter(roles))


async def _facilitator_org_ids(
    db,
    user_sub: str,
) -> frozenset[uuid.UUID]:
    rows = await db.execute(
        select(FacilitatorOrgMembership.facilitator_org_id).where(
            FacilitatorOrgMembership.user_subject_id == user_sub
        )
    )
    return frozenset(rows.scalars().all())


async def _assert_facilitator_org_scope(
    db,
    user_sub: str,
    contact_id: uuid.UUID,
) -> None:
    """F2: a facilitator-role actor invoking the generic transition endpoint
    on a contact whose only open match is in a different org is silently
    permitted by the state machine (matched → sent_back is gated on role,
    not org). Mirror the decide_match org-scope check here.

    Behavior:
      * load the facilitator's org memberships;
      * load every open Match for any of the contact's interests;
      * if no open match's ``facilitator_org_id`` is in the membership set,
        raise 403.

    If the contact has no open matches at all, fall through — there's
    nothing to scope to and the state-machine's role gate will produce a
    more precise 403 / 409 downstream.
    """
    org_ids = await _facilitator_org_ids(db, user_sub)
    open_match_orgs = (
        await db.execute(
            select(Match.facilitator_org_id)
            .join(
                AdopterInterest,
                AdopterInterest.id == Match.adopter_interest_id,
            )
            .where(AdopterInterest.contact_id == contact_id)
            .where(Match.status.in_(OPEN_MATCH_STATUSES))
        )
    ).scalars().all()
    if not open_match_orgs:
        return
    if not (org_ids & set(open_match_orgs)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "org_not_member",
                "message": (
                    "Caller is not a member of any org with an open match for "
                    "this contact"
                ),
            },
        )


@router.post("/{contact_id}/transition", response_model=TransitionResponse)
async def post_transition(
    contact_id: uuid.UUID,
    body: TransitionRequest,
    db: DbSession,
    user_with_roles: CurrentUserWithRoles,
    _: Annotated[tuple[object, frozenset[str]], Depends(_workflow_dep)],
) -> TransitionResponse:
    user, roles = user_with_roles
    contact = await db.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found"
        )

    # F2: org-scope guard for a facilitator-only actor invoking the generic
    # transition endpoint. Mirrors the decide_match org-scope check so a
    # facilitator in org-A can't drive the state machine on a contact whose
    # only open match is in org-B. ``triage_facilitator`` alone also gets
    # the check: it has no legitimate org-scoped reason to drive transitions
    # via this endpoint.
    staff_roles = frozenset({"staff_admin", "adoption_manager"})
    if not (roles & staff_roles):
        await _assert_facilitator_org_scope(db, user.sub, contact_id)

    # Validate the target state literal before attempting the transition, so
    # callers see a precise 400 rather than a 500 from a bad enum lookup.
    # F19: previously a same-state body was short-circuited to a 200 no-op
    # *before* the role / org check ran — a probing facilitator could
    # confirm that a contact had reached a particular state without being a
    # member of the relevant org. Drop the early return: the state machine
    # rejects self-loops as IllegalTransitionError and we map that to 409.
    if body.kind == "adopter":
        try:
            AdopterState(body.to_state)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "invalid_state",
                    "message": f"Unknown adopter state: {body.to_state!r}",
                },
            ) from e
    else:
        try:
            FacilitatorState(body.to_state)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "invalid_state",
                    "message": f"Unknown facilitator state: {body.to_state!r}",
                },
            ) from e

    actor_role = _pick_actor_role(roles)

    try:
        if body.kind == "adopter":
            await transition_adopter(
                db,
                contact,
                to_state=AdopterState(body.to_state),
                actor_b2c_sub=user.sub,
                actor_role=actor_role,
                reason_code=body.reason_code,
                reason_text=body.reason_text,
            )
        else:
            await transition_facilitator(
                db,
                contact,
                to_state=FacilitatorState(body.to_state),
                actor_b2c_sub=user.sub,
                actor_role=actor_role,
                reason_code=body.reason_code,
                reason_text=body.reason_text,
            )
    except (
        ReasonRequiredError,
        InvalidReasonCodeError,
        RoleNotPermittedError,
        IllegalTransitionError,
        ConcurrentModificationError,
    ) as e:
        await db.rollback()
        raise map_state_machine_exception(e) from e

    await db.commit()
    await db.refresh(contact)
    return TransitionResponse(
        contact=ContactRead.model_validate(contact),
        transitioned_to=body.to_state,
    )
