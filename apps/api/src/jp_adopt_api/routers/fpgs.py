"""People-group (FPG) lookup — a typeahead over the ``fpg`` reference table so
staff can add an FPG selection to a contact by searching its name or people_id3.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select

from jp_adopt_api.deps import STAFF_ROLES, DbSession, require_role
from jp_adopt_api.models import Fpg

router = APIRouter(prefix="/v1/fpgs", tags=["fpgs"])

_staff_dep = require_role(*STAFF_ROLES)


class FpgRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    people_id3: str
    name: str
    country_code: str | None = None
    frontier: bool


class FpgListResponse(BaseModel):
    items: list[FpgRead]


@router.get("", response_model=FpgListResponse)
async def search_fpgs(
    db: DbSession,
    _user: Annotated[tuple[object, frozenset[str]], Depends(_staff_dep)],
    q: Annotated[
        str | None,
        Query(description="Case-insensitive substring of name or people_id3."),
    ] = None,
    limit: int = Query(20, ge=1, le=50),
) -> FpgListResponse:
    stmt = select(Fpg)
    q_trimmed = q.strip() if q else ""
    if q_trimmed:
        escaped = (
            q_trimmed.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        stmt = stmt.where(
            or_(
                Fpg.name.ilike(pattern, escape="\\"),
                Fpg.people_id3.ilike(pattern, escape="\\"),
            )
        )
    # Frontier groups first (the program's focus), then by name.
    stmt = stmt.order_by(Fpg.frontier.desc(), Fpg.name.asc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return FpgListResponse(items=[FpgRead.model_validate(r) for r in rows])
