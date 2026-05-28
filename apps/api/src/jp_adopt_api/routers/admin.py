"""Staff-admin endpoints (U8 follow-up, F15; Entra Part F user_roles).

Facilitator onboarding and platform role assignment share this router, all
gated on ``require_role('staff_admin')``:

  * ``GET  /v1/facilitating-orgs``               — list active orgs.
  * ``POST /v1/admin/facilitator-memberships``   — grant facilitator-org access.
  * ``DELETE /v1/admin/facilitator-memberships/{user_sub}/{org_id}`` — revoke.
  * ``GET  /v1/admin/roles``                   — list assignable platform roles.
  * ``GET  /v1/admin/user-roles``              — list current user_roles grants.
  * ``POST /v1/admin/user-roles``              — grant a platform role (outbox).
  * ``DELETE /v1/admin/user-roles/{user_sub}/{role_id}`` — revoke (outbox).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

import sqlalchemy
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from jp_adopt_api.auth import AuthUser
from jp_adopt_api.deps import DbSession, require_role
from jp_adopt_api.models import FacilitatingOrg, FacilitatorOrgMembership, Role, UserRole
from jp_adopt_api.outbox_suppression import emit_outbox

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

    user_subject_id: str = Field(min_length=1, max_length=256)
    facilitator_org_id: uuid.UUID
    role_in_org: str = Field(default="member", pattern=r"^(member|admin)$")


class FacilitatorMembershipRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_subject_id: str
    facilitator_org_id: uuid.UUID
    role_in_org: str


class RoleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None = None


class RoleListResponse(BaseModel):
    items: list[RoleRead]
    total: int


class UserRoleRead(BaseModel):
    user_subject_id: str
    role_id: uuid.UUID
    role_name: str
    granted_at: datetime


class UserRoleListResponse(BaseModel):
    items: list[UserRoleRead]
    total: int


_ENTRA_OID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class UserRoleGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_subject_id: str = Field(
        min_length=1,
        max_length=256,
        pattern=_ENTRA_OID_PATTERN,
    )
    role_id: uuid.UUID


async def _user_role_read(
    db: DbSession, *, user_subject_id: str, role_id: uuid.UUID
) -> UserRoleRead | None:
    row = (
        await db.execute(
            select(UserRole, Role.name)
            .join(Role, Role.id == UserRole.role_id)
            .where(
                UserRole.user_subject_id == user_subject_id,
                UserRole.role_id == role_id,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    ur, role_name = row
    return UserRoleRead(
        user_subject_id=ur.user_subject_id,
        role_id=ur.role_id,
        role_name=role_name,
        granted_at=ur.granted_at,
    )


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
        user_subject_id=body.user_subject_id,
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
    "/v1/admin/facilitator-memberships/{user_subject_id}/{facilitator_org_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_facilitator_membership(
    user_subject_id: str,
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
            FacilitatorOrgMembership.user_subject_id == user_subject_id,
            FacilitatorOrgMembership.facilitator_org_id == facilitator_org_id,
        )
    )
    await db.commit()
    return None


@router.get(
    "/v1/admin/roles",
    response_model=RoleListResponse,
)
async def list_roles(
    db: DbSession,
    _: Annotated[
        tuple[object, frozenset[str]], Depends(_staff_admin_dep)
    ],
) -> RoleListResponse:
    """List platform roles for the admin grant dropdown."""
    rows = (
        await db.execute(select(Role).order_by(Role.name.asc()))
    ).scalars().all()
    items = [RoleRead.model_validate(r) for r in rows]
    return RoleListResponse(items=items, total=len(items))


@router.get(
    "/v1/admin/user-roles",
    response_model=UserRoleListResponse,
)
async def list_user_roles(
    db: DbSession,
    _: Annotated[
        tuple[object, frozenset[str]], Depends(_staff_admin_dep)
    ],
) -> UserRoleListResponse:
    """List all ``user_roles`` grants joined with role names."""
    rows = (
        await db.execute(
            select(UserRole, Role.name)
            .join(Role, Role.id == UserRole.role_id)
            .order_by(UserRole.granted_at.desc())
        )
    ).all()
    items = [
        UserRoleRead(
            user_subject_id=ur.user_subject_id,
            role_id=ur.role_id,
            role_name=role_name,
            granted_at=ur.granted_at,
        )
        for ur, role_name in rows
    ]
    return UserRoleListResponse(items=items, total=len(items))


@router.post(
    "/v1/admin/user-roles",
    response_model=UserRoleRead,
)
async def grant_user_role(
    body: UserRoleGrantRequest,
    db: DbSession,
    actor: Annotated[tuple[AuthUser, frozenset[str]], Depends(_staff_admin_dep)],
) -> UserRoleRead:
    """Grant a platform role to an Entra OID. Idempotent on the composite PK."""
    user, _roles = actor
    role = await db.get(Role, body.role_id)
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "role_not_found",
                "message": f"No role with id {body.role_id}",
            },
        )

    await db.execute(
        pg_insert(UserRole)
        .values(
            user_subject_id=body.user_subject_id,
            role_id=body.role_id,
        )
        .on_conflict_do_nothing(
            index_elements=["user_subject_id", "role_id"],
        )
    )
    emit_outbox(
        db,
        event_type="admin.role.granted",
        payload={
            "actor_subject_id": user.sub,
            "target_subject_id": body.user_subject_id,
            "role_id": str(role.id),
            "role_name": role.name,
        },
    )
    await db.commit()

    read = await _user_role_read(
        db, user_subject_id=body.user_subject_id, role_id=body.role_id
    )
    assert read is not None
    return read


@router.delete(
    "/v1/admin/user-roles/{user_subject_id}/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_user_role(
    user_subject_id: str,
    role_id: uuid.UUID,
    db: DbSession,
    actor: Annotated[tuple[AuthUser, frozenset[str]], Depends(_staff_admin_dep)],
) -> None:
    """Revoke a platform role grant. Refuses self-revoke of ``staff_admin``."""
    user, _roles = actor
    role = await db.get(Role, role_id)
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "role_not_found",
                "message": f"No role with id {role_id}",
            },
        )

    existing = (
        await db.execute(
            select(UserRole).where(
                UserRole.user_subject_id == user_subject_id,
                UserRole.role_id == role_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "user_role_not_found",
                "message": "No grant for this user and role",
            },
        )

    if user_subject_id == user.sub and role.name == "staff_admin":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "self_revoke_forbidden",
                "message": "Cannot revoke your own staff_admin role",
            },
        )

    await db.delete(existing)
    emit_outbox(
        db,
        event_type="admin.role.revoked",
        payload={
            "actor_subject_id": user.sub,
            "target_subject_id": user_subject_id,
            "role_id": str(role.id),
            "role_name": role.name,
        },
    )
    await db.commit()
    return None
