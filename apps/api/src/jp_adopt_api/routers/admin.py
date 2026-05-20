"""Staff-admin endpoints (U8 follow-up, F15).

Onboarding a facilitator currently requires a direct DB insert into
``facilitating_org`` and ``facilitator_org_membership``. Three thin
``require_role('staff_admin')`` endpoints close that gap:

  * ``GET  /v1/facilitating-orgs``               — list active orgs.
  * ``POST /v1/admin/facilitator-memberships``   — grant a B2C subject access
                                                   to one facilitator org.
  * ``DELETE /v1/admin/facilitator-memberships/{user_sub}/{org_id}`` —
                                                   revoke that grant.

Everything more ambitious — full CRUD on orgs, bulk-membership import, etc.
is a v2 concern.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select

from jp_adopt_api.deps import DbSession, require_role
from jp_adopt_api.models import FacilitatingOrg, FacilitatorOrgMembership

router = APIRouter(tags=["admin"])


_staff_admin_dep = require_role("staff_admin")


# ──────────────────────────────────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────────────────────────────────


class FacilitatingOrgRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    country_code: str | None = None
    capacity_total: int
    capacity_committed: int
    active: bool
    is_triage_org: bool


class FacilitatingOrgListResponse(BaseModel):
    items: list[FacilitatingOrgRead]
    total: int


class FacilitatorMembershipCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_b2c_subject_id: str = Field(min_length=1, max_length=256)
    facilitator_org_id: uuid.UUID
    role_in_org: str = Field(default="member", pattern=r"^(member|admin)$")


class FacilitatorMembershipRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_b2c_subject_id: str
    facilitator_org_id: uuid.UUID
    role_in_org: str


# ──────────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────────


@router.get(
    "/v1/facilitating-orgs",
    response_model=FacilitatingOrgListResponse,
)
async def list_facilitating_orgs(
    db: DbSession,
    _: Annotated[
        tuple[object, frozenset[str]], Depends(_staff_admin_dep)
    ],
) -> FacilitatingOrgListResponse:
    """List active facilitating orgs. Ordering is alphabetical by ``name``
    for stable display."""
    rows = (
        await db.execute(
            select(FacilitatingOrg)
            .where(FacilitatingOrg.active.is_(True))
            .order_by(FacilitatingOrg.name.asc())
        )
    ).scalars().all()
    items = [FacilitatingOrgRead.model_validate(r) for r in rows]
    return FacilitatingOrgListResponse(items=items, total=len(items))


@router.post(
    "/v1/admin/facilitator-memberships",
    response_model=FacilitatorMembershipRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_facilitator_membership(
    body: FacilitatorMembershipCreateRequest,
    db: DbSession,
    _: Annotated[
        tuple[object, frozenset[str]], Depends(_staff_admin_dep)
    ],
) -> FacilitatorMembershipRead:
    """Grant a B2C subject access to one facilitator org. 409 if the grant
    already exists (the table has a composite primary key)."""
    org = await db.get(FacilitatingOrg, body.facilitator_org_id)
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "facilitating_org_not_found",
                "message": f"No facilitating_org with id {body.facilitator_org_id}",
            },
        )

    membership = FacilitatorOrgMembership(
        user_b2c_subject_id=body.user_b2c_subject_id,
        facilitator_org_id=body.facilitator_org_id,
        role_in_org=body.role_in_org,
    )
    db.add(membership)
    try:
        await db.commit()
    except sqlalchemy.exc.IntegrityError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "membership_already_exists",
                "message": "This user already has membership in this org",
            },
        ) from e
    return FacilitatorMembershipRead.model_validate(membership)


@router.delete(
    "/v1/admin/facilitator-memberships/{user_b2c_subject_id}/{facilitator_org_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_facilitator_membership(
    user_b2c_subject_id: str,
    facilitator_org_id: uuid.UUID,
    db: DbSession,
    _: Annotated[
        tuple[object, frozenset[str]], Depends(_staff_admin_dep)
    ],
) -> None:
    """Revoke a facilitator-org membership. Idempotent: returns 204 whether
    or not the row existed (so re-runs of an onboarding script are safe)."""
    await db.execute(
        delete(FacilitatorOrgMembership).where(
            FacilitatorOrgMembership.user_b2c_subject_id == user_b2c_subject_id,
            FacilitatorOrgMembership.facilitator_org_id == facilitator_org_id,
        )
    )
    await db.commit()
    return None
