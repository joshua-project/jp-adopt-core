from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jp_adopt_api.auth import AuthUser
from jp_adopt_api.deps import CurrentUser, DbSession
from jp_adopt_api.models import Contact, Outbox
from jp_adopt_api.schemas import ContactListResponse, ContactPatch, ContactRead

router = APIRouter(prefix="/v1/contacts", tags=["contacts"])

EVENT_CONTACT_UPDATED = "jp.adopt.v1.contact.updated"


@router.get("", response_model=ContactListResponse)
async def list_contacts(
    db: DbSession,
    _user: CurrentUser,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> ContactListResponse:
    total = int((await db.execute(select(func.count()).select_from(Contact))).scalar_one())
    result = await db.execute(
        select(Contact).order_by(Contact.created_at).offset(offset).limit(limit)
    )
    rows = result.scalars().all()
    return ContactListResponse(
        items=[ContactRead.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
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
    if "adopter_status" in updates:
        contact.adopter_status = updates["adopter_status"]
    if "facilitator_status" in updates:
        contact.facilitator_status = updates["facilitator_status"]
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
    outbox = Outbox(
        id=uuid.uuid4(),
        event_type=EVENT_CONTACT_UPDATED,
        payload_json=payload,
    )
    db.add(outbox)

    await db.commit()
    await db.refresh(contact)
    return ContactRead.model_validate(contact)
