"""Suppression-list admin endpoints (F3 / #55).

The drip worker hard-filters sends against ``suppression_list`` at send time
(see ``apps/api/src/jp_adopt_api/domain/drips.py``). Until now, staff could
only mutate that table via direct SQL. These endpoints close the loop:

  * ``GET    /v1/suppression-list``               — paginated list
  * ``POST   /v1/suppression-list``               — add (idempotent)
  * ``DELETE /v1/suppression-list/{email_hash}``  — remove

Gating: ``{staff_admin, adoption_manager}`` — the standard staff set used
by ``contacts`` / ``drips`` / ``manual_contacts``. Suppression is an
operational task (spam complaints, manual unsubscribes), not strictly an
admin function, so it does not gate on ``staff_admin`` alone.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import delete, func, select

from jp_adopt_api.deps import DbSession, require_role
from jp_adopt_api.domain.drips import (
    add_to_suppression_list,
    email_hash as compute_email_hash,
)
from jp_adopt_api.models import SuppressionList

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/suppression-list", tags=["suppression"])

_STAFF_ROLES = frozenset({"staff_admin", "adoption_manager"})
_STAFF_DEP = require_role(*_STAFF_ROLES)


class SuppressionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    email_hash: str
    reason: str
    suppressed_at: datetime
    source_metadata: dict[str, Any] | None = None


class SuppressionListResponse(BaseModel):
    items: list[SuppressionRead]
    total: int


class SuppressionCreate(BaseModel):
    """Request body for ``POST /v1/suppression-list``. The server normalizes
    and hashes the email; the raw address is never persisted."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    reason: str = Field(default="manual", min_length=1, max_length=64)
    source_metadata: dict[str, Any] | None = None


@router.get("", response_model=SuppressionListResponse)
async def list_suppression(
    db: DbSession,
    _: Annotated[tuple[object, frozenset[str]], Depends(_STAFF_DEP)],
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> SuppressionListResponse:
    total = int(
        (
            await db.execute(select(func.count()).select_from(SuppressionList))
        ).scalar_one()
    )
    rows = (
        await db.execute(
            select(SuppressionList)
            .order_by(SuppressionList.suppressed_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return SuppressionListResponse(
        items=[SuppressionRead.model_validate(r) for r in rows],
        total=total,
    )


@router.post("", response_model=SuppressionRead, status_code=status.HTTP_200_OK)
async def add_suppression(
    body: SuppressionCreate,
    db: DbSession,
    _: Annotated[tuple[object, frozenset[str]], Depends(_STAFF_DEP)],
) -> SuppressionRead:
    """Add an email to the suppression list. Idempotent — re-adding the same
    address returns the existing row at 200, not 409 (per F3 KTD-4)."""
    await add_to_suppression_list(
        db,
        email=body.email,
        reason=body.reason,
        source_metadata=body.source_metadata,
    )
    await db.commit()
    h = compute_email_hash(body.email)
    row = await db.get(SuppressionList, h)
    if row is None:  # pragma: no cover — invariant after the upsert above
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "suppression_insert_failed"},
        )
    return SuppressionRead.model_validate(row)


@router.delete("/{email_hash}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_suppression(
    email_hash: str,
    db: DbSession,
    _: Annotated[tuple[object, frozenset[str]], Depends(_STAFF_DEP)],
) -> None:
    result = await db.execute(
        delete(SuppressionList).where(SuppressionList.email_hash == email_hash)
    )
    if (result.rowcount or 0) == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "suppression_not_found",
                "message": "No suppression entry with that hash",
            },
        )
    await db.commit()
