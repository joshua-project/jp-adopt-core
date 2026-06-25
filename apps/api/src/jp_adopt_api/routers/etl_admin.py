"""Staff-admin endpoints for ETL observability.

These complement the hourly DT-sync Container Apps Job by giving agents
(and admin operators) an HTTP surface for the three audit tables the cron
writes to. Every endpoint is gated on ``require_role('staff_admin')``:

  * ``GET /v1/admin/etl-runs``                       — list etl_run rows
  * ``GET /v1/admin/migration-conflicts``            — list conflict rows
  * ``GET /v1/admin/migration-conflicts/summary``    — aggregate counts
  * ``GET /v1/admin/etl-deleted-in-source``          — vanished-from-source

The corresponding ``POST /v1/admin/etl/trigger`` (kick off the cron
out-of-band via Azure Container Apps Job Start) is a follow-up — requires
``azure-identity`` on the API + a managed-identity role assignment on
the job. For now operators trigger via ``az containerapp job start``
(see ``docs/runbooks/dt-cron-sync.md``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from jp_adopt_api.auth import AuthUser
from jp_adopt_api.deps import DbSession, require_role
from jp_adopt_api.models import (
    Contact,
    DuplicateReviewDecision,
    EtlDeletedInSource,
    EtlRun,
    MigrationConflict,
)

_FORBIDDEN_RESPONSE: dict[int | str, dict[str, Any]] = {
    403: {"description": "Caller lacks the staff_admin role"},
}

router = APIRouter(tags=["admin"])


_staff_admin_dep = require_role("staff_admin")


# ──────────────────────────────────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────────────────────────────────


class EtlRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    table_name: str
    mode: str
    started_at: datetime
    ended_at: datetime | None
    watermark_from: datetime | None
    source_max_modified_at: datetime | None
    rows_in: int
    rows_out_inserted: int
    rows_out_updated: int
    rows_out_skipped: int
    rows_in_conflict: int
    errors: int


class EtlRunListResponse(BaseModel):
    items: list[EtlRunRead]
    total: int


class MigrationConflictRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_system: str
    source_id: str
    table_name: str
    conflict_type: str
    source_value: dict[str, Any] | None = None
    local_value: dict[str, Any] | None = None
    detected_at: datetime


class MigrationConflictListResponse(BaseModel):
    items: list[MigrationConflictRead]
    total: int


class MigrationConflictSummaryRow(BaseModel):
    """One ``(table_name, conflict_type)`` bucket plus its count.

    Cheaper than the full list when an agent or operator only wants the
    breakdown (matches the ``SELECT conflict_type, COUNT(*) ... GROUP BY``
    runbook query).
    """

    table_name: str
    conflict_type: str
    count: int


class MigrationConflictSummaryResponse(BaseModel):
    items: list[MigrationConflictSummaryRow]
    total: int


class EtlDeletedInSourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    etl_run_id: uuid.UUID
    table_name: str
    source_system: str
    source_id: str
    last_seen_at: datetime | None
    detected_at: datetime


class EtlDeletedInSourceListResponse(BaseModel):
    items: list[EtlDeletedInSourceRead]
    total: int


# ──────────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────────


_LIMIT_DEFAULT = 100
_LIMIT_MAX = 500


@router.get(
    "/v1/admin/etl-runs",
    response_model=EtlRunListResponse,
    responses=_FORBIDDEN_RESPONSE,
)
async def list_etl_runs(
    db: DbSession,
    _: Annotated[
        tuple[AuthUser, frozenset[str]], Depends(_staff_admin_dep)
    ],
    table_name: str | None = None,
    mode: str | None = None,
    since: datetime | None = None,
    has_errors: bool | None = None,
    limit: int = Query(_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
) -> EtlRunListResponse:
    """List ETL audit-run rows, newest first.

    All filters are optional and AND-combined. ``has_errors=true`` keeps
    only rows with ``errors > 0``; ``has_errors=false`` keeps only
    ``errors = 0`` rows. ``since`` filters by ``started_at >= since``.
    ``total`` is the count BEFORE the limit was applied so a paginated
    client can show "M of N".
    """
    stmt = select(EtlRun)
    if table_name is not None:
        stmt = stmt.where(EtlRun.table_name == table_name)
    if mode is not None:
        stmt = stmt.where(EtlRun.mode == mode)
    if since is not None:
        stmt = stmt.where(EtlRun.started_at >= since)
    if has_errors is True:
        stmt = stmt.where(EtlRun.errors > 0)
    elif has_errors is False:
        stmt = stmt.where(EtlRun.errors == 0)

    total = (
        await db.execute(
            select(func.count()).select_from(stmt.subquery())
        )
    ).scalar_one()

    rows = (
        await db.execute(
            stmt.order_by(EtlRun.started_at.desc()).limit(limit)
        )
    ).scalars().all()

    return EtlRunListResponse(
        items=[EtlRunRead.model_validate(r) for r in rows],
        total=total,
    )


def _conflict_where_clauses(
    source_system: str | None,
    table_name: str | None,
    conflict_type: str | None,
    since: datetime | None,
) -> list[Any]:
    """Compose the filter set both /migration-conflicts endpoints share."""
    clauses: list[Any] = []
    if source_system is not None:
        clauses.append(MigrationConflict.source_system == source_system)
    if table_name is not None:
        clauses.append(MigrationConflict.table_name == table_name)
    if conflict_type is not None:
        clauses.append(MigrationConflict.conflict_type == conflict_type)
    if since is not None:
        clauses.append(MigrationConflict.detected_at >= since)
    return clauses


@router.get(
    "/v1/admin/migration-conflicts",
    response_model=MigrationConflictListResponse,
    responses=_FORBIDDEN_RESPONSE,
)
async def list_migration_conflicts(
    db: DbSession,
    _: Annotated[
        tuple[AuthUser, frozenset[str]], Depends(_staff_admin_dep)
    ],
    source_system: str | None = None,
    table_name: str | None = None,
    conflict_type: str | None = None,
    since: datetime | None = None,
    limit: int = Query(_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
) -> MigrationConflictListResponse:
    """List migration_conflicts rows newest-first.

    For grouped counts (the ``SELECT conflict_type, COUNT(*) GROUP BY``
    shape from the cron runbook), use the sibling endpoint
    ``/v1/admin/migration-conflicts/summary`` instead — its ``total``
    field is the sum of per-bucket counts, which differs from the
    pre-limit row count this endpoint returns.
    """
    stmt = select(MigrationConflict).where(
        *_conflict_where_clauses(source_system, table_name, conflict_type, since)
    )
    total = (
        await db.execute(
            select(func.count()).select_from(stmt.subquery())
        )
    ).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(MigrationConflict.detected_at.desc()).limit(limit)
        )
    ).scalars().all()
    return MigrationConflictListResponse(
        items=[MigrationConflictRead.model_validate(r) for r in rows],
        total=total,
    )


@router.get(
    "/v1/admin/migration-conflicts/summary",
    response_model=MigrationConflictSummaryResponse,
    responses=_FORBIDDEN_RESPONSE,
)
async def summarize_migration_conflicts(
    db: DbSession,
    _: Annotated[
        tuple[AuthUser, frozenset[str]], Depends(_staff_admin_dep)
    ],
    source_system: str | None = None,
    table_name: str | None = None,
    conflict_type: str | None = None,
    since: datetime | None = None,
) -> MigrationConflictSummaryResponse:
    """Return aggregate counts grouped by ``(table_name, conflict_type)``.

    ``total`` is the sum of per-bucket counts — i.e. the total
    matching conflict rows — NOT a pre-limit row count. This endpoint
    has no ``limit`` parameter; the bucket set is bounded by the
    number of distinct (table, conflict_type) pairs.
    """
    summary_stmt = (
        select(
            MigrationConflict.table_name,
            MigrationConflict.conflict_type,
            func.count().label("count"),
        )
        .where(
            *_conflict_where_clauses(
                source_system, table_name, conflict_type, since
            )
        )
        .group_by(
            MigrationConflict.table_name, MigrationConflict.conflict_type
        )
        .order_by(
            MigrationConflict.table_name, MigrationConflict.conflict_type
        )
    )
    rows = (await db.execute(summary_stmt)).all()
    items = [
        MigrationConflictSummaryRow(
            table_name=r.table_name,
            conflict_type=r.conflict_type,
            count=r.count,
        )
        for r in rows
    ]
    return MigrationConflictSummaryResponse(
        items=items, total=sum(i.count for i in items)
    )


@router.get(
    "/v1/admin/etl-deleted-in-source",
    response_model=EtlDeletedInSourceListResponse,
    responses=_FORBIDDEN_RESPONSE,
)
async def list_etl_deleted_in_source(
    db: DbSession,
    _: Annotated[
        tuple[AuthUser, frozenset[str]], Depends(_staff_admin_dep)
    ],
    source_system: str | None = None,
    table_name: str | None = None,
    since: datetime | None = None,
    limit: int = Query(_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
) -> EtlDeletedInSourceListResponse:
    """List rows that vanished from the source on a prior full ETL run.

    These rows were never hard-deleted in jp-adopt-core — Amy reviews
    each one and decides per case. Filters mirror the cron runbook's
    reconciliation query.
    """
    stmt = select(EtlDeletedInSource)
    if source_system is not None:
        stmt = stmt.where(EtlDeletedInSource.source_system == source_system)
    if table_name is not None:
        stmt = stmt.where(EtlDeletedInSource.table_name == table_name)
    if since is not None:
        stmt = stmt.where(EtlDeletedInSource.detected_at >= since)

    total = (
        await db.execute(
            select(func.count()).select_from(stmt.subquery())
        )
    ).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(EtlDeletedInSource.detected_at.desc()).limit(limit)
        )
    ).scalars().all()
    return EtlDeletedInSourceListResponse(
        items=[EtlDeletedInSourceRead.model_validate(r) for r in rows],
        total=total,
    )


# ──────────────────────────────────────────────────────────────────────────
# Duplicate-email review — the staff "Review duplicates" UI
# ──────────────────────────────────────────────────────────────────────────
#
# A ``duplicate_email`` conflict is a DT-origin contact whose email collided
# with an existing (usually forms-intake) contact during import. Track A
# auto-merges the high-confidence name+email matches; the ambiguous remainder
# sits here for a human to judge. These endpoints surface each conflict as a
# PAIR (the DT contact ↔ the email owner) and record the reviewer's call in
# ``duplicate_review_decision`` — ``merge`` is applied by the next hourly Track
# A run (``--decisions-from-db``); ``ignore`` hides shared-inbox false positives.

_DUP_CONFLICT_TYPE = "duplicate_email"


class DuplicateConflictContact(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    display_name: str
    adopter_status: str | None = None
    email_normalized: str | None = None
    source_system: str | None = None
    source_id: str | None = None
    created_at: datetime | None = None


class DuplicateConflictItem(BaseModel):
    email: str
    dt_source_id: str
    detected_at: datetime
    # How many DT records collide on this email. >1 ⇒ likely a shared inbox
    # (different people), where at most ONE can be the merge keeper.
    cluster_size: int
    decision: Literal["merge", "ignore"] | None = None
    dt_contact: DuplicateConflictContact | None = None
    owner_contact: DuplicateConflictContact | None = None


class DuplicateConflictListResponse(BaseModel):
    items: list[DuplicateConflictItem]
    total: int


class DuplicateDecisionRequest(BaseModel):
    email: str
    dt_source_id: str
    decision: Literal["merge", "ignore"]


class DuplicateDecisionResponse(BaseModel):
    email: str
    dt_source_id: str
    decision: Literal["merge", "ignore"]


def _dup_contact_payload(
    contact: Contact | None,
) -> DuplicateConflictContact | None:
    return (
        DuplicateConflictContact.model_validate(contact)
        if contact is not None
        else None
    )


@router.get(
    "/v1/admin/duplicate-conflicts",
    response_model=DuplicateConflictListResponse,
    responses=_FORBIDDEN_RESPONSE,
)
async def list_duplicate_conflicts(
    db: DbSession,
    _: Annotated[
        tuple[AuthUser, frozenset[str]], Depends(_staff_admin_dep)
    ],
    include_ignored: bool = False,
    limit: int = Query(_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
) -> DuplicateConflictListResponse:
    """List unresolved ``duplicate_email`` conflicts as reviewable pairs.

    Each item carries the DT-origin contact, the contact that owns the email
    (the merge target), the cluster size (how many DT records share the
    email), and any pending reviewer decision. ``ignore``-d conflicts are
    hidden unless ``include_ignored=true``. ``merge``-d ones remain (shown as
    queued) until the next Track A run applies and deletes them.
    """
    conflicts = (
        await db.execute(
            select(MigrationConflict).where(
                MigrationConflict.conflict_type == _DUP_CONFLICT_TYPE
            )
        )
    ).scalars().all()

    # Cluster sizes (email → number of colliding DT records).
    cluster: dict[str, int] = {}
    for c in conflicts:
        email = (c.source_value or {}).get("email_normalized")
        if email:
            cluster[email] = cluster.get(email, 0) + 1

    decisions = {
        (d.email_normalized, d.dt_source_id): d.decision
        for d in (
            await db.execute(select(DuplicateReviewDecision))
        ).scalars().all()
    }

    dt_ids = [c.source_id for c in conflicts if c.source_id]
    emails = [
        e
        for c in conflicts
        if (e := (c.source_value or {}).get("email_normalized"))
    ]
    dt_by_id: dict[str, Contact] = {}
    if dt_ids:
        dt_by_id = {
            r.source_id: r
            for r in (
                await db.execute(
                    select(Contact).where(
                        Contact.source_system == "dt",
                        Contact.source_id.in_(dt_ids),
                    )
                )
            ).scalars().all()
            if r.source_id
        }
    owner_by_email: dict[str, Contact] = {}
    if emails:
        owner_by_email = {
            r.email_normalized: r
            for r in (
                await db.execute(
                    select(Contact).where(Contact.email_normalized.in_(emails))
                )
            ).scalars().all()
            if r.email_normalized
        }

    items: list[DuplicateConflictItem] = []
    for c in conflicts:
        email = (c.source_value or {}).get("email_normalized")
        if not email:
            continue
        decision = decisions.get((email, c.source_id))
        if decision == "ignore" and not include_ignored:
            continue
        items.append(
            DuplicateConflictItem(
                email=email,
                dt_source_id=c.source_id,
                detected_at=c.detected_at,
                cluster_size=cluster.get(email, 1),
                decision=decision,
                dt_contact=_dup_contact_payload(dt_by_id.get(c.source_id)),
                owner_contact=_dup_contact_payload(owner_by_email.get(email)),
            )
        )

    # Undecided first (the reviewer's actual work), then group by email so
    # shared-inbox clusters sit together.
    items.sort(key=lambda i: (i.decision is not None, i.email, i.dt_source_id))
    return DuplicateConflictListResponse(items=items[:limit], total=len(items))


@router.post(
    "/v1/admin/duplicate-conflicts/decide",
    response_model=DuplicateDecisionResponse,
    responses=_FORBIDDEN_RESPONSE,
)
async def decide_duplicate_conflict(
    db: DbSession,
    body: DuplicateDecisionRequest,
    actor: Annotated[
        tuple[AuthUser, frozenset[str]], Depends(_staff_admin_dep)
    ],
) -> DuplicateDecisionResponse:
    """Record a reviewer's call on one ``duplicate_email`` conflict.

    ``merge`` queues the DT-authoritative merge for the next Track A run;
    ``ignore`` hides a shared-inbox false positive. Idempotent upsert keyed on
    ``(email, dt_source_id)``. For a ``merge`` in a shared-email cluster only
    ONE keeper is allowed, so any other ``merge`` on the same email is cleared.
    """
    user = actor[0]
    decided_by = user.email or user.sub
    if body.decision == "merge":
        await db.execute(
            delete(DuplicateReviewDecision).where(
                DuplicateReviewDecision.email_normalized == body.email,
                DuplicateReviewDecision.dt_source_id != body.dt_source_id,
                DuplicateReviewDecision.decision == "merge",
            )
        )
    await db.execute(
        pg_insert(DuplicateReviewDecision)
        .values(
            email_normalized=body.email,
            dt_source_id=body.dt_source_id,
            decision=body.decision,
            decided_by=decided_by,
        )
        .on_conflict_do_update(
            constraint="uq_duplicate_review_decision_conflict",
            set_={
                "decision": body.decision,
                "decided_by": decided_by,
                "decided_at": func.now(),
            },
        )
    )
    await db.commit()
    return DuplicateDecisionResponse(
        email=body.email,
        dt_source_id=body.dt_source_id,
        decision=body.decision,
    )


@router.delete(
    "/v1/admin/duplicate-conflicts/decide",
    status_code=204,
    responses=_FORBIDDEN_RESPONSE,
)
async def clear_duplicate_decision(
    db: DbSession,
    _: Annotated[
        tuple[AuthUser, frozenset[str]], Depends(_staff_admin_dep)
    ],
    email: str = Query(...),
    dt_source_id: str = Query(...),
) -> None:
    """Undo a prior decision so the conflict returns to the review list."""
    await db.execute(
        delete(DuplicateReviewDecision).where(
            DuplicateReviewDecision.email_normalized == email,
            DuplicateReviewDecision.dt_source_id == dt_source_id,
        )
    )
    await db.commit()
