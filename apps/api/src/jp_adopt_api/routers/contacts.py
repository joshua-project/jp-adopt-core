from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from jp_adopt_api.deps import CurrentUser, DbSession
from jp_adopt_api.models import Contact
from jp_adopt_api.outbox_suppression import emit_outbox
from jp_adopt_api.schemas import (
    ContactListResponse,
    ContactPatch,
    ContactRead,
    ContactStatusCounts,
)

router = APIRouter(prefix="/v1/contacts", tags=["contacts"])

EVENT_CONTACT_UPDATED = "jp.adopt.v1.contact.updated"

# Allowed status values per kind — mirrors the CHECK constraints on the
# ``contacts`` table (see migration 0001 + models.py). Keeping the lists
# here lets the route reject invalid filter values with a 422 *before*
# the query runs, instead of returning an empty result set for typos.
_ADOPTER_STATUSES = (
    "draft",
    "new",
    "potential_adopter",
    "contacted",
    "engaged",
    "matched",
    "sent_back",
    "active",
    "inactive",
    "do_not_engage",
)
_FACILITATOR_STATUSES = (
    "draft",
    "new",
    "not_ready",
    "ready",
    "do_not_engage",
)
_UNSET_KEY = "__unset__"


@router.get("", response_model=ContactListResponse)
async def list_contacts(
    db: DbSession,
    _user: CurrentUser,
    # Pipeline views (/adopters, /facilitators) request limit=200 so the
    # kanban can show everything in one shot without paging — most JP
    # cohorts are well under 500 contacts total. Bump max to 500 to give
    # those views room without enabling DoS-shaped queries.
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    party_kind: Annotated[
        Literal["adopter", "facilitator"] | None,
        Query(description="Restrict to one party kind."),
    ] = None,
    adopter_status: Annotated[
        list[str] | None,
        Query(
            description=(
                "Filter adopters by status. Repeatable: "
                "?adopter_status=new&adopter_status=matched. "
                "Ignored when party_kind=facilitator."
            ),
        ),
    ] = None,
    facilitator_status: Annotated[
        list[str] | None,
        Query(
            description=(
                "Filter facilitators by status. Repeatable. "
                "Ignored when party_kind=adopter."
            ),
        ),
    ] = None,
) -> ContactListResponse:
    # Validate status values against the known enum so a typo gets a 422
    # right away instead of silently returning an empty list.
    if adopter_status:
        bad = [s for s in adopter_status if s not in _ADOPTER_STATUSES]
        if bad:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "unknown_adopter_status",
                    "message": f"Unknown adopter_status values: {bad}",
                    "allowed": list(_ADOPTER_STATUSES),
                },
            )
    if facilitator_status:
        bad = [s for s in facilitator_status if s not in _FACILITATOR_STATUSES]
        if bad:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "unknown_facilitator_status",
                    "message": (
                        f"Unknown facilitator_status values: {bad}"
                    ),
                    "allowed": list(_FACILITATOR_STATUSES),
                },
            )

    # Build the filtered query. Use the same conditions for the count
    # so the response total matches the filtered set, not the table size.
    conditions = []
    if party_kind is not None:
        conditions.append(Contact.party_kind == party_kind)
    if party_kind == "adopter" and adopter_status:
        conditions.append(Contact.adopter_status.in_(adopter_status))
    if party_kind == "facilitator" and facilitator_status:
        conditions.append(Contact.facilitator_status.in_(facilitator_status))

    count_stmt = select(func.count()).select_from(Contact)
    list_stmt = select(Contact).order_by(Contact.created_at)
    for c in conditions:
        count_stmt = count_stmt.where(c)
        list_stmt = list_stmt.where(c)

    total = int((await db.execute(count_stmt)).scalar_one())
    rows = (
        await db.execute(list_stmt.offset(offset).limit(limit))
    ).scalars().all()
    return ContactListResponse(
        items=[ContactRead.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/status_counts", response_model=ContactStatusCounts)
async def contact_status_counts(
    db: DbSession,
    _user: CurrentUser,
    party_kind: Annotated[
        Literal["adopter", "facilitator"],
        Query(
            description=(
                "Which party kind to count by status. Required — the two "
                "kinds have different status enums and the response shape "
                "depends on it."
            ),
        ),
    ],
) -> ContactStatusCounts:
    """Aggregate status counts for the pipeline filter chips.

    Returns ``{counts: {status: n, ...}, total: N}`` for the requested
    party kind. NULL statuses are aggregated under ``__unset__`` so the
    UI can render a "no status" bucket without losing rows.

    Why a separate endpoint instead of computing client-side from
    ``/v1/contacts``: that endpoint paginates (default limit=50), so a
    client can't sum statuses by walking the list. Sums need a server-
    side aggregate, full stop.
    """
    status_col = (
        Contact.adopter_status
        if party_kind == "adopter"
        else Contact.facilitator_status
    )

    rows = (
        await db.execute(
            select(status_col, func.count())
            .where(Contact.party_kind == party_kind)
            .group_by(status_col)
        )
    ).all()

    counts: dict[str, int] = {}
    total = 0
    for value, n in rows:
        key = value if value is not None else _UNSET_KEY
        counts[key] = int(n)
        total += int(n)

    return ContactStatusCounts(
        party_kind=party_kind,
        counts=counts,
        total=total,
    )


@router.get("/{contact_id}", response_model=ContactRead)
async def get_contact(
    contact_id: uuid.UUID,
    db: DbSession,
    _user: CurrentUser,
) -> ContactRead:
    row = await db.get(Contact, contact_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found")
    return ContactRead.model_validate(row)


@router.patch("/{contact_id}", response_model=ContactRead)
async def patch_contact(
    contact_id: uuid.UUID,
    body: ContactPatch,
    db: DbSession,
    _user: CurrentUser,
) -> ContactRead:
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No mutable fields provided",
        )
    contact = await db.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found")

    now = datetime.now(timezone.utc)
    if "party_kind" in updates:
        contact.party_kind = updates["party_kind"]
    if "display_name" in updates:
        contact.display_name = updates["display_name"]
    # NOTE: adopter_status / facilitator_status were intentionally removed
    # from ContactPatch in F5: status mutations must flow through the
    # state-machine entrypoints (transition_adopter / transition_facilitator)
    # to enforce role checks, reason-code validation, and the audit row.
    # A follow-up unit (U7) will add ``POST /v1/contacts/{id}/transition``;
    # until then, this PATCH endpoint covers display_name / party_kind only.
    contact.updated_at = now

    payload = {
        "event": EVENT_CONTACT_UPDATED,
        "schema_version": "jp.adopt.v1",
        "timestamp": now.isoformat(),
        "contact_id": str(contact.id),
        "data": {
            "display_name": contact.display_name,
            "party_kind": contact.party_kind,
            "adopter_status": contact.adopter_status,
            "facilitator_status": contact.facilitator_status,
        },
    }
    # Route via emit_outbox so bulk-import paths can suppress this event
    # under their summary; outside suppression, this writes a normal Outbox
    # row (preserving the previous behavior).
    emit_outbox(
        db,
        event_type=EVENT_CONTACT_UPDATED,
        payload=payload,
    )

    await db.commit()
    await db.refresh(contact)
    return ContactRead.model_validate(contact)
