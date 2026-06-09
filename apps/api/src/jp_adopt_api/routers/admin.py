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
from jp_adopt_api.graph import (
    graph_configured,
    lookup_users_by_ids,
    search_users,
)
from jp_adopt_api.models import (
    FacilitatingOrg,
    FacilitatorFpgCoverage,
    FacilitatorOrgMembership,
    Fpg,
    Match,
    Role,
    UserRole,
)
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


# #57: admin facilitating-org management. Distinct from the public
# `FacilitatingOrgRead` above so /v1/facilitating-orgs stays small
# (used by match-review widgets); the admin surface exposes the
# whole row plus derived fields.


class FacilitatingOrgAdminRead(BaseModel):
    """Full admin view of a facilitating org row.

    `capacity_remaining` is derived; the matching algorithm owns
    `capacity_committed` and rejects external writes (see
    PATCH below).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    country_code: str | None = None
    language_codes: list[str] | None = None
    capacity_total: int
    capacity_committed: int
    capacity_remaining: int
    accepting_potential_adopters: bool
    is_triage_org: bool
    active: bool
    source_system: str | None = None
    source_id: str | None = None
    created_at: datetime
    updated_at: datetime


class FacilitatingOrgAdminListResponse(BaseModel):
    items: list[FacilitatingOrgAdminRead]
    total: int


class FacilitatingOrgCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=512)
    country_code: str | None = Field(default=None, max_length=8)
    capacity_total: int = Field(default=0, ge=0)
    accepting_potential_adopters: bool = False
    is_triage_org: bool = False


class FacilitatingOrgPatch(BaseModel):
    """Patch payload. `capacity_committed` is *intentionally* absent —
    the matching algorithm is the only writer for that column."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=512)
    country_code: str | None = Field(default=None, max_length=8)
    capacity_total: int | None = Field(default=None, ge=0)
    accepting_potential_adopters: bool | None = None
    is_triage_org: bool | None = None


class FpgCoverageRead(BaseModel):
    """One row in the `facilitator_fpg_coverage` join table, with the
    FPG name pre-resolved so the UI doesn't have to round-trip."""

    people_id3: str
    name: str | None = None
    country_code: str | None = None


class FpgCoverageAddRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    people_id3: str = Field(min_length=1, max_length=16)


class FpgCoverageListResponse(BaseModel):
    items: list[FpgCoverageRead]


class FacilitatingOrgDetail(BaseModel):
    """Single-org view: row + FPG coverage + memberships in one trip."""

    model_config = ConfigDict(from_attributes=False)

    org: FacilitatingOrgAdminRead
    coverage: list[FpgCoverageRead]
    memberships: list["FacilitatorMembershipRead"]


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
    # Graph enrichment (#97). Both are best-effort: when Graph is
    # unconfigured or the OID isn't resolvable they stay None and
    # the UI falls back to displaying the OID.
    user_display_name: str | None = None
    user_principal_name: str | None = None


class UserRoleListResponse(BaseModel):
    items: list[UserRoleRead]
    total: int
    # `True` when the Graph backend is wired and was reachable for
    # this response. UI surfaces use this to decide whether to show
    # an "enable Graph for friendlier names" hint or just render
    # OIDs silently. False here is not an error.
    graph_enriched: bool = False


class UserSearchHit(BaseModel):
    """One row in the admin user-search typeahead response."""

    user_subject_id: str
    display_name: str | None
    user_principal_name: str | None
    mail: str | None


class UserSearchResponse(BaseModel):
    items: list[UserSearchHit]
    # `graph_configured` is False in dev where the AZURE_GRAPH_*
    # env vars aren't set. The UI shows an inline notice in that
    # case rather than a flat empty result.
    graph_configured: bool


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
    """List all ``user_roles`` grants joined with role names.

    When Graph is configured (#97), each row is enriched with the
    ``user_display_name`` and ``user_principal_name`` fields via a
    single batched Graph call. Failures (timeout, missing user,
    unconfigured) degrade silently to OID-only output.
    """
    rows = (
        await db.execute(
            select(UserRole, Role.name)
            .join(Role, Role.id == UserRole.role_id)
            .order_by(UserRole.granted_at.desc())
        )
    ).all()
    oids = list({ur.user_subject_id for ur, _name in rows if ur.user_subject_id})
    graph_users = await lookup_users_by_ids(oids) if oids else {}
    items = [
        UserRoleRead(
            user_subject_id=ur.user_subject_id,
            role_id=ur.role_id,
            role_name=role_name,
            granted_at=ur.granted_at,
            user_display_name=(
                graph_users[ur.user_subject_id].display_name
                if ur.user_subject_id in graph_users
                else None
            ),
            user_principal_name=(
                graph_users[ur.user_subject_id].user_principal_name
                if ur.user_subject_id in graph_users
                else None
            ),
        )
        for ur, role_name in rows
    ]
    return UserRoleListResponse(
        items=items,
        total=len(items),
        graph_enriched=bool(graph_users),
    )


@router.get(
    "/v1/admin/users/search",
    response_model=UserSearchResponse,
)
async def search_directory_users(
    q: str,
    _: Annotated[
        tuple[object, frozenset[str]], Depends(_staff_admin_dep)
    ],
) -> UserSearchResponse:
    """Typeahead search for Entra users by name / email prefix.

    Used by the admin UI's grant form so operators can type a name
    instead of pasting an OID. Returns ``[]`` (with ``graph_configured=False``)
    in dev environments where the AZURE_GRAPH_* env vars aren't set,
    so the UI can fall back to the raw OID input gracefully.
    """
    hits = await search_users(q)
    items = [
        UserSearchHit(
            user_subject_id=u.id,
            display_name=u.display_name,
            user_principal_name=u.user_principal_name,
            mail=u.mail,
        )
        for u in hits
    ]
    return UserSearchResponse(items=items, graph_configured=graph_configured())


@router.post(
    "/v1/admin/user-roles",
    response_model=UserRoleRead,
    status_code=status.HTTP_201_CREATED,
)
async def grant_user_role(
    body: UserRoleGrantRequest,
    db: DbSession,
    actor: Annotated[tuple[AuthUser, frozenset[str]], Depends(_staff_admin_dep)],
) -> UserRoleRead:
    """Grant a platform role to an Entra OID. Idempotent on the composite PK:
    re-granting an existing (subject, role) pair returns 201 with the existing
    row and does NOT emit a second outbox event (the outbox represents real
    state changes, not request attempts)."""
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

    insert_result = await db.execute(
        pg_insert(UserRole)
        .values(
            user_subject_id=body.user_subject_id,
            role_id=body.role_id,
        )
        .on_conflict_do_nothing(
            index_elements=["user_subject_id", "role_id"],
        )
    )
    # rowcount is 0 when the (subject, role) row already existed and ON CONFLICT
    # absorbed the insert. Only emit when we actually changed state, so the
    # audit log reflects real grants — not duplicate POSTs from a refresh or
    # a retry.
    if insert_result.rowcount > 0:
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


# ──────────────────────────────────────────────────────────────────────────
# #57: Admin facilitating-org management
# ──────────────────────────────────────────────────────────────────────────


# Match states that block deactivation. Matches outside this set
# (declined, completed, sent_back, withdrawn) are terminal and don't
# pin the org.
_OPEN_MATCH_STATUSES = frozenset({"recommended", "accepted", "active", "triage"})


def _admin_read(row: FacilitatingOrg) -> FacilitatingOrgAdminRead:
    return FacilitatingOrgAdminRead(
        id=row.id,
        name=row.name,
        country_code=row.country_code,
        language_codes=row.language_codes,
        capacity_total=row.capacity_total,
        capacity_committed=row.capacity_committed,
        capacity_remaining=max(0, row.capacity_total - row.capacity_committed),
        accepting_potential_adopters=row.accepting_potential_adopters,
        is_triage_org=row.is_triage_org,
        active=row.active,
        source_system=row.source_system,
        source_id=row.source_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _load_org(db: DbSession, org_id: uuid.UUID) -> FacilitatingOrg:
    row = (
        await db.execute(
            select(FacilitatingOrg).where(FacilitatingOrg.id == org_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "org_not_found", "message": "No such org"},
        )
    return row


async def _coverage_for(
    db: DbSession, org_id: uuid.UUID
) -> list[FpgCoverageRead]:
    rows = (
        await db.execute(
            select(FacilitatorFpgCoverage.people_id3, Fpg.name, Fpg.country_code)
            .join(Fpg, Fpg.people_id3 == FacilitatorFpgCoverage.people_id3, isouter=True)
            .where(FacilitatorFpgCoverage.facilitator_org_id == org_id)
            .order_by(FacilitatorFpgCoverage.people_id3.asc())
        )
    ).all()
    return [
        FpgCoverageRead(people_id3=p, name=n, country_code=c) for p, n, c in rows
    ]


async def _memberships_for(
    db: DbSession, org_id: uuid.UUID
) -> list[FacilitatorMembershipRead]:
    rows = (
        await db.execute(
            select(FacilitatorOrgMembership).where(
                FacilitatorOrgMembership.facilitator_org_id == org_id
            )
        )
    ).scalars().all()
    return [FacilitatorMembershipRead.model_validate(r) for r in rows]


@router.get(
    "/v1/admin/facilitating-orgs",
    response_model=FacilitatingOrgAdminListResponse,
)
async def admin_list_facilitating_orgs(
    db: DbSession,
    _: Annotated[
        tuple[object, frozenset[str]], Depends(_staff_admin_dep)
    ],
) -> FacilitatingOrgAdminListResponse:
    """List every org (including inactive) for the admin surface.

    Ordering: active first, then name ascending. The public
    `/v1/facilitating-orgs` endpoint excludes inactive rows; this
    admin variant returns the whole table so staff can reactivate.
    """
    rows = (
        await db.execute(
            select(FacilitatingOrg).order_by(
                FacilitatingOrg.active.desc(), FacilitatingOrg.name.asc()
            )
        )
    ).scalars().all()
    items = [_admin_read(r) for r in rows]
    return FacilitatingOrgAdminListResponse(items=items, total=len(items))


@router.post(
    "/v1/admin/facilitating-orgs",
    response_model=FacilitatingOrgAdminRead,
    status_code=status.HTTP_201_CREATED,
)
async def admin_create_facilitating_org(
    body: FacilitatingOrgCreate,
    db: DbSession,
    auth: Annotated[
        tuple[AuthUser, frozenset[str]], Depends(_staff_admin_dep)
    ],
) -> FacilitatingOrgAdminRead:
    user, _ = auth
    # is_triage_org has a partial unique index — at most one triage
    # org may exist. Surface this as 409 rather than letting the
    # IntegrityError propagate as 500.
    if body.is_triage_org:
        existing_triage = (
            await db.execute(
                select(FacilitatingOrg.id).where(
                    FacilitatingOrg.is_triage_org.is_(True)
                )
            )
        ).scalar_one_or_none()
        if existing_triage is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "triage_org_exists",
                    "message": "Only one triage org is allowed",
                },
            )
    org = FacilitatingOrg(
        id=uuid.uuid4(),
        name=body.name.strip(),
        country_code=body.country_code,
        capacity_total=body.capacity_total,
        accepting_potential_adopters=body.accepting_potential_adopters,
        is_triage_org=body.is_triage_org,
        active=True,
    )
    db.add(org)
    await db.flush()
    emit_outbox(
        db,
        event_type="jp.adopt.v1.facilitating_org.created",
        payload={
            "actor_subject_id": user.sub,
            "facilitator_org_id": str(org.id),
            "name": org.name,
        },
    )
    await db.commit()
    await db.refresh(org)
    return _admin_read(org)


@router.get(
    "/v1/admin/facilitating-orgs/{org_id}",
    response_model=FacilitatingOrgDetail,
)
async def admin_get_facilitating_org(
    org_id: uuid.UUID,
    db: DbSession,
    _: Annotated[
        tuple[object, frozenset[str]], Depends(_staff_admin_dep)
    ],
) -> FacilitatingOrgDetail:
    org = await _load_org(db, org_id)
    return FacilitatingOrgDetail(
        org=_admin_read(org),
        coverage=await _coverage_for(db, org_id),
        memberships=await _memberships_for(db, org_id),
    )


@router.patch(
    "/v1/admin/facilitating-orgs/{org_id}",
    response_model=FacilitatingOrgAdminRead,
)
async def admin_patch_facilitating_org(
    org_id: uuid.UUID,
    body: FacilitatingOrgPatch,
    db: DbSession,
    auth: Annotated[
        tuple[AuthUser, frozenset[str]], Depends(_staff_admin_dep)
    ],
) -> FacilitatingOrgAdminRead:
    user, _ = auth
    org = await _load_org(db, org_id)
    # Apply only the supplied fields. `capacity_committed` is not in
    # `FacilitatingOrgPatch`, so it can't be set here — matching is
    # the sole writer.
    update_fields = body.model_dump(exclude_unset=True)
    if not update_fields:
        return _admin_read(org)
    # Refuse to drop capacity_total below the current committed count,
    # which would violate the CHECK constraint and surface as a 500.
    if "capacity_total" in update_fields and update_fields["capacity_total"] < org.capacity_committed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "capacity_below_committed",
                "message": (
                    f"capacity_total={update_fields['capacity_total']} would "
                    f"be less than capacity_committed={org.capacity_committed}"
                ),
            },
        )
    # Triage uniqueness mirror of the create-time guard.
    if update_fields.get("is_triage_org") is True and not org.is_triage_org:
        existing_triage = (
            await db.execute(
                select(FacilitatingOrg.id).where(
                    FacilitatingOrg.is_triage_org.is_(True),
                    FacilitatingOrg.id != org_id,
                )
            )
        ).scalar_one_or_none()
        if existing_triage is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "triage_org_exists",
                    "message": "Only one triage org is allowed",
                },
            )
    for k, v in update_fields.items():
        setattr(org, k, v)
    emit_outbox(
        db,
        event_type="jp.adopt.v1.facilitating_org.updated",
        payload={
            "actor_subject_id": user.sub,
            "facilitator_org_id": str(org.id),
            "fields": sorted(update_fields.keys()),
        },
    )
    await db.commit()
    await db.refresh(org)
    return _admin_read(org)


@router.post(
    "/v1/admin/facilitating-orgs/{org_id}/deactivate",
    response_model=FacilitatingOrgAdminRead,
)
async def admin_deactivate_facilitating_org(
    org_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[
        tuple[AuthUser, frozenset[str]], Depends(_staff_admin_dep)
    ],
) -> FacilitatingOrgAdminRead:
    user, _ = auth
    org = await _load_org(db, org_id)
    open_match_count = (
        await db.execute(
            select(sqlalchemy.func.count(Match.id)).where(
                Match.facilitator_org_id == org_id,
                Match.status.in_(_OPEN_MATCH_STATUSES),
            )
        )
    ).scalar_one()
    if open_match_count > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "org_has_open_matches",
                "message": (
                    f"Cannot deactivate — {open_match_count} open match(es) "
                    "reference this org. Resolve them first."
                ),
                "open_match_count": int(open_match_count),
            },
        )
    if org.active:
        org.active = False
        emit_outbox(
            db,
            event_type="jp.adopt.v1.facilitating_org.deactivated",
            payload={
                "actor_subject_id": user.sub,
                "facilitator_org_id": str(org.id),
            },
        )
        await db.commit()
        await db.refresh(org)
    return _admin_read(org)


@router.post(
    "/v1/admin/facilitating-orgs/{org_id}/activate",
    response_model=FacilitatingOrgAdminRead,
)
async def admin_activate_facilitating_org(
    org_id: uuid.UUID,
    db: DbSession,
    auth: Annotated[
        tuple[AuthUser, frozenset[str]], Depends(_staff_admin_dep)
    ],
) -> FacilitatingOrgAdminRead:
    user, _ = auth
    org = await _load_org(db, org_id)
    if not org.active:
        org.active = True
        emit_outbox(
            db,
            event_type="jp.adopt.v1.facilitating_org.activated",
            payload={
                "actor_subject_id": user.sub,
                "facilitator_org_id": str(org.id),
            },
        )
        await db.commit()
        await db.refresh(org)
    return _admin_read(org)


@router.get(
    "/v1/admin/facilitating-orgs/{org_id}/coverage",
    response_model=FpgCoverageListResponse,
)
async def admin_list_coverage(
    org_id: uuid.UUID,
    db: DbSession,
    _: Annotated[
        tuple[object, frozenset[str]], Depends(_staff_admin_dep)
    ],
) -> FpgCoverageListResponse:
    await _load_org(db, org_id)
    return FpgCoverageListResponse(items=await _coverage_for(db, org_id))


@router.post(
    "/v1/admin/facilitating-orgs/{org_id}/coverage",
    response_model=FpgCoverageRead,
    status_code=status.HTTP_201_CREATED,
)
async def admin_add_coverage(
    org_id: uuid.UUID,
    body: FpgCoverageAddRequest,
    db: DbSession,
    auth: Annotated[
        tuple[AuthUser, frozenset[str]], Depends(_staff_admin_dep)
    ],
) -> FpgCoverageRead:
    user, _ = auth
    await _load_org(db, org_id)
    # Verify the FPG exists so we 404 on a typo rather than persist a
    # dangling join row (FK is RESTRICT but the message is friendlier).
    fpg_row = (
        await db.execute(
            select(Fpg.people_id3, Fpg.name, Fpg.country_code).where(
                Fpg.people_id3 == body.people_id3
            )
        )
    ).one_or_none()
    if fpg_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "fpg_not_found",
                "message": f"No FPG with people_id3={body.people_id3}",
            },
        )
    insert_result = await db.execute(
        pg_insert(FacilitatorFpgCoverage)
        .values(facilitator_org_id=org_id, people_id3=body.people_id3)
        .on_conflict_do_nothing(
            index_elements=["facilitator_org_id", "people_id3"]
        )
    )
    if insert_result.rowcount > 0:
        emit_outbox(
            db,
            event_type="jp.adopt.v1.facilitating_org.coverage_added",
            payload={
                "actor_subject_id": user.sub,
                "facilitator_org_id": str(org_id),
                "people_id3": body.people_id3,
            },
        )
    await db.commit()
    return FpgCoverageRead(
        people_id3=fpg_row.people_id3,
        name=fpg_row.name,
        country_code=fpg_row.country_code,
    )


@router.delete(
    "/v1/admin/facilitating-orgs/{org_id}/coverage/{people_id3}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def admin_remove_coverage(
    org_id: uuid.UUID,
    people_id3: str,
    db: DbSession,
    auth: Annotated[
        tuple[AuthUser, frozenset[str]], Depends(_staff_admin_dep)
    ],
) -> None:
    user, _ = auth
    await _load_org(db, org_id)
    result = await db.execute(
        delete(FacilitatorFpgCoverage).where(
            FacilitatorFpgCoverage.facilitator_org_id == org_id,
            FacilitatorFpgCoverage.people_id3 == people_id3,
        )
    )
    if result.rowcount == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "coverage_not_found",
                "message": "No such coverage row",
            },
        )
    emit_outbox(
        db,
        event_type="jp.adopt.v1.facilitating_org.coverage_removed",
        payload={
            "actor_subject_id": user.sub,
            "facilitator_org_id": str(org_id),
            "people_id3": people_id3,
        },
    )
    await db.commit()
    return None
