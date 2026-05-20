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
from jp_adopt_api.models import Contact
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

    # Idempotent retry: a transition request that lands on the same target
    # state as the contact is currently in is treated as a no-op 200 rather
    # than an IllegalTransitionError (the state machine refuses self-loops).
    if body.kind == "adopter":
        try:
            target = AdopterState(body.to_state)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "invalid_state",
                    "message": f"Unknown adopter state: {body.to_state!r}",
                },
            ) from e
        if contact.adopter_status == target.value:
            return TransitionResponse(
                contact=ContactRead.model_validate(contact),
                transitioned_to=target.value,
            )
    else:
        try:
            fac_target = FacilitatorState(body.to_state)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "invalid_state",
                    "message": f"Unknown facilitator state: {body.to_state!r}",
                },
            ) from e
        if contact.facilitator_status == fac_target.value:
            return TransitionResponse(
                contact=ContactRead.model_validate(contact),
                transitioned_to=fac_target.value,
            )

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
        raise _map_state_machine_exception(e) from e

    await db.commit()
    await db.refresh(contact)
    return TransitionResponse(
        contact=ContactRead.model_validate(contact),
        transitioned_to=body.to_state,
    )
