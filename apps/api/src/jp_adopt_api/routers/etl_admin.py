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
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from jp_adopt_api.auth import AuthUser
from jp_adopt_api.deps import DbSession, require_role
from jp_adopt_api.models import EtlDeletedInSource, EtlRun, MigrationConflict

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
