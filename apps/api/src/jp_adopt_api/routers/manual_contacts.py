"""Manual contact creation router (U11).

Staff-only endpoint for Amy / adoption_manager to add a contact by
hand (e.g. a phone walk-in, an event attendee, a referral that didn't
come through Form A/B). Distinct from ``/v1/intake/*`` which is the
public form-receiver — manual creates auto-tag ``origin='manual_entry'``
and skip the Idempotency-Key bookkeeping.

Endpoint:
  * ``POST /v1/contacts/manual`` body:
    {display_name, email, party_kind, origin?, country_code?,
     language_codes?, fpg_people_id3s?, facilitator_org_id?}
  → creates Contact + N AdopterInterest rows (one per people_id3) +
    optional Match row when facilitator_org_id is provided.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from jp_adopt_api.deps import CurrentUserWithRoles, DbSession, require_role
from jp_adopt_api.email_utils import normalize_email
from jp_adopt_api.models import (
    AdopterInterest,
    Contact,
    FacilitatingOrg,
    Fpg,
    Match,
)
from jp_adopt_api.outbox_suppression import emit_outbox

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/contacts", tags=["contacts"])

_STAFF_ROLES = frozenset({"staff_admin", "adoption_manager"})
_MANUAL_DEP = require_role(*_STAFF_ROLES)


ORIGIN_VALUES = (
    "core_org",
    "website",
    "third_party_referral",
    "partner_event",
    "manual_entry",
    "other",
)


# ──────────────────────────────────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────────────────────────────────


class ManualContactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=512)
    email: EmailStr
    party_kind: Literal["adopter", "facilitator"] = "adopter"
    origin: str | None = Field(default="manual_entry", max_length=64)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    language_codes: list[str] | None = None
    newsletter_opt_in: bool = False
    # FPG selections (people_id3 codes) — used to create AdopterInterest rows.
    # Empty list ⇒ contact lands as 'potential_adopter' (no FPG yet),
    # mirroring the intake endpoint's behavior.
    fpg_people_id3s: list[str] = Field(default_factory=list, max_length=20)
    # Optional facilitator assignment — when provided, creates an
    # initial Match row in 'recommended' status pointing to the org.
    # The matching algorithm in U6 doesn't run for manual creates;
    # staff explicitly picked the org.
    facilitator_org_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def normalize(self) -> Self:
        if self.origin and self.origin not in ORIGIN_VALUES:
            raise ValueError(
                f"origin must be one of {ORIGIN_VALUES}, got {self.origin!r}"
            )
        if self.country_code:
            object.__setattr__(
                self, "country_code", self.country_code.upper()
            )
        if self.language_codes:
            object.__setattr__(
                self,
                "language_codes",
                [c.strip().lower() for c in self.language_codes if c.strip()],
            )
        # Dedup people_id3s (staff might paste the same code twice)
        if self.fpg_people_id3s:
            seen: set[str] = set()
            cleaned: list[str] = []
            for r in self.fpg_people_id3s:
                r2 = r.strip()
                if r2 and r2 not in seen:
                    cleaned.append(r2)
                    seen.add(r2)
            object.__setattr__(self, "fpg_people_id3s", cleaned)
        return self


class ManualContactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    contact_id: uuid.UUID
    interest_ids: list[uuid.UUID]
    match_id: uuid.UUID | None
    contact_status: str | None
    created: bool  # False when an existing contact with this email was reused


# ──────────────────────────────────────────────────────────────────────────
# Endpoint
# ──────────────────────────────────────────────────────────────────────────


EVENT_MANUAL_CONTACT_CREATED = "jp.adopt.v1.contact.manual_created"


@router.post(
    "/manual",
    response_model=ManualContactResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_manual_contact(
    body: ManualContactCreate,
    db: DbSession,
    user_with_roles: CurrentUserWithRoles,
    _: Annotated[
        tuple[object, frozenset[str]], Depends(_MANUAL_DEP)
    ],
) -> ManualContactResponse:
    actor, _roles = user_with_roles
    email_normalized = normalize_email(body.email)

    # Validate every fpg people_id3 exists before any inserts so partial
    # success doesn't leave an orphan AdopterInterest behind.
    if body.fpg_people_id3s:
        found = (
            await db.execute(
                select(Fpg.people_id3).where(Fpg.people_id3.in_(body.fpg_people_id3s))
            )
        ).scalars().all()
        missing = sorted(set(body.fpg_people_id3s) - set(found))
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "unknown_fpg_people_id3",
                    "message": f"Unknown people_id3 codes: {missing}",
                    "fields": {"fpg_people_id3s": missing},
                },
            )

    # Validate facilitator_org_id exists + is active when provided.
    facilitator_org: FacilitatingOrg | None = None
    if body.facilitator_org_id is not None:
        facilitator_org = await db.get(FacilitatingOrg, body.facilitator_org_id)
        if facilitator_org is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="facilitator_org_id not found",
            )
        if not facilitator_org.active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "facilitator_inactive",
                    "message": "Cannot assign to an inactive facilitating org",
                },
            )

    # Resolve / create contact by normalized email (same as intake).
    existing = (
        await db.execute(
            select(Contact).where(Contact.email_normalized == email_normalized)
        )
    ).scalar_one_or_none()

    if existing is not None:
        # Reuse — but never silently re-flag newsletter_opt_in down.
        contact = existing
        created = False
        if body.newsletter_opt_in and not contact.newsletter_opt_in:
            contact.newsletter_opt_in = True
    else:
        initial_adopter = "new" if body.party_kind == "adopter" else None
        initial_fac = "new" if body.party_kind == "facilitator" else None
        if body.party_kind == "adopter" and not body.fpg_people_id3s:
            initial_adopter = "potential_adopter"
        contact = Contact(
            id=uuid.uuid4(),
            party_kind=body.party_kind,
            display_name=body.display_name,
            adopter_status=initial_adopter,
            facilitator_status=initial_fac,
            email_normalized=email_normalized,
            origin=body.origin or "manual_entry",
            newsletter_opt_in=body.newsletter_opt_in,
            country_code=body.country_code,
            language_codes=body.language_codes,
        )
        db.add(contact)
        await db.flush()
        created = True

    # Create AdopterInterest rows. Multi-FPG ⇒ one per people_id3. Zero ⇒
    # single people_id3=NULL row so the matcher's no-FPG triage path fires.
    interest_ids: list[uuid.UUID] = []
    if body.party_kind == "adopter":
        people_id3s = body.fpg_people_id3s or [None]  # type: ignore[list-item]
        for people_id3 in people_id3s:
            interest = AdopterInterest(
                id=uuid.uuid4(),
                contact_id=contact.id,
                people_id3=people_id3,
                notes=body.notes,
            )
            db.add(interest)
            await db.flush()
            interest_ids.append(interest.id)

    # Create a Match row when a facilitator is pre-assigned. Only valid
    # for adopter party_kind + at least one AdopterInterest. Use the
    # rank-1 interest as the FK target.
    match_id: uuid.UUID | None = None
    if facilitator_org is not None and interest_ids:
        match = Match(
            id=uuid.uuid4(),
            adopter_interest_id=interest_ids[0],
            facilitator_org_id=facilitator_org.id,
            status="recommended",
        )
        try:
            async with db.begin_nested():
                db.add(match)
                await db.flush()
            match_id = match.id
        except IntegrityError:
            # Conflict on uq_match_open_per_interest — the contact already
            # has an open match (likely from a prior intake submission).
            # Re-fetch + return its id rather than 500'ing.
            existing_match = (
                await db.execute(
                    select(Match.id)
                    .where(
                        Match.adopter_interest_id == interest_ids[0],
                        Match.status.in_(
                            ("recommended", "accepted", "active", "triage")
                        ),
                    )
                )
            ).scalar_one_or_none()
            match_id = existing_match

    # Outbox event so downstream consumers (drip engine, integrations)
    # treat this like any other contact creation.
    emit_outbox(
        db,
        event_type=EVENT_MANUAL_CONTACT_CREATED,
        payload={
            "event": EVENT_MANUAL_CONTACT_CREATED,
            "schema_version": "jp.adopt.v1",
            "contact_id": str(contact.id),
            "interest_ids": [str(i) for i in interest_ids],
            "match_id": str(match_id) if match_id else None,
            "party_kind": body.party_kind,
            "origin": contact.origin,
            "newsletter_opt_in": contact.newsletter_opt_in,
            "actor": {"sub": actor.sub},
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )

    await db.commit()

    return ManualContactResponse(
        contact_id=contact.id,
        interest_ids=interest_ids,
        match_id=match_id,
        contact_status=(
            contact.adopter_status
            if body.party_kind == "adopter"
            else contact.facilitator_status
        ),
        created=created,
    )
