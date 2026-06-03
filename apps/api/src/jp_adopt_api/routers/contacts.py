from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, select

from jp_adopt_api.deps import DbSession, SettingsDep, require_role
from jp_adopt_api.models import (
    ActivityLog,
    AdopterInterest,
    Campaign,
    Contact,
    ContactAssignment,
    ContactProfile,
    Enrollment,
    FacilitatingOrg,
    Fpg,
    Match,
    TransitionAudit,
)
from jp_adopt_api.outbox_suppression import emit_outbox
from jp_adopt_api.schemas import (
    ContactActivityResponse,
    ContactActivityRow,
    ContactAssignmentRequest,
    ContactEmailCreate,
    ContactEmailResponse,
    ContactEnrollmentRow,
    ContactEnrollmentsResponse,
    ContactListResponse,
    ContactMatchesResponse,
    ContactMatchRow,
    ContactNoteCreate,
    ContactPatch,
    ContactProfileRead,
    ContactRead,
    ContactStatusCounts,
    ContactTimelineEntry,
    ContactTimelineResponse,
    ContactTransitionRow,
    ContactTransitionsResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/contacts", tags=["contacts"])

EVENT_CONTACT_UPDATED = "jp.adopt.v1.contact.updated"

# Staff roles allowed to read/write contacts. Mirrors `manual_contacts._STAFF_ROLES`
# — the set of staff users who triage adopter/facilitator records. Without this
# gate, `partner_tenants` membership (tenant-level) admits any JP-tenant Entra
# account; the role check (row-level) is the second defense gate (U22 of the
# Entra direct plan).
_STAFF_ROLES = frozenset({"staff_admin", "adoption_manager"})
_STAFF_DEP = require_role(*_STAFF_ROLES)

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
    _user: Annotated[tuple[object, frozenset[str]], Depends(_STAFF_DEP)],
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
    _user: Annotated[tuple[object, frozenset[str]], Depends(_STAFF_DEP)],
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


async def _contact_read_with_profile(
    db: DbSession, contact: Contact
) -> ContactRead:
    """Build ContactRead and attach the 1:1 contact_profile (U9), if present."""
    read = ContactRead.model_validate(contact)
    prof = (
        await db.execute(
            select(ContactProfile).where(ContactProfile.contact_id == contact.id)
        )
    ).scalar_one_or_none()
    if prof is not None:
        read.profile = ContactProfileRead.model_validate(prof)
    asn = (
        await db.execute(
            select(ContactAssignment).where(
                ContactAssignment.contact_id == contact.id
            )
        )
    ).scalar_one_or_none()
    if asn is not None:
        read.assigned_to = asn.user_subject_id
    return read


@router.get("/{contact_id}", response_model=ContactRead)
async def get_contact(
    contact_id: uuid.UUID,
    db: DbSession,
    _user: Annotated[tuple[object, frozenset[str]], Depends(_STAFF_DEP)],
) -> ContactRead:
    row = await db.get(Contact, contact_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found")
    return await _contact_read_with_profile(db, row)


# ── Contact record (U1/U2): per-contact aggregates + add-note ──────────────
#
# Role-gated to staff via _STAFF_DEP, consistent with the sibling contact
# endpoints after the Entra-direct auth overhaul.


async def _require_contact(db: DbSession, contact_id: uuid.UUID) -> Contact:
    contact = await db.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found"
        )
    return contact


@router.get("/{contact_id}/matches", response_model=ContactMatchesResponse)
async def get_contact_matches(
    contact_id: uuid.UUID,
    db: DbSession,
    _user: Annotated[tuple[object, frozenset[str]], Depends(_STAFF_DEP)],
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ContactMatchesResponse:
    """All Matches across this contact's AdopterInterests (newest first).

    The matches API is otherwise keyed on AdopterInterest; the record page
    needs the whole-contact view, so we join through interests here.
    """
    await _require_contact(db, contact_id)
    interest_join = AdopterInterest.id == Match.adopter_interest_id
    total = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Match)
                .join(AdopterInterest, interest_join)
                .where(AdopterInterest.contact_id == contact_id)
            )
        ).scalar_one()
    )
    rows = (
        await db.execute(
            select(
                Match,
                AdopterInterest.people_id3,
                FacilitatingOrg.name,
                Fpg.name,
                Fpg.country_code,
            )
            .join(AdopterInterest, interest_join)
            .join(FacilitatingOrg, FacilitatingOrg.id == Match.facilitator_org_id)
            .outerjoin(Fpg, Fpg.people_id3 == AdopterInterest.people_id3)
            .where(AdopterInterest.contact_id == contact_id)
            .order_by(Match.recommended_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    items = [
        ContactMatchRow(
            id=m.id,
            adopter_interest_id=m.adopter_interest_id,
            people_id3=people_id3,
            people_id3_name=fpg_name,
            people_id3_country=fpg_country,
            facilitator_org_id=m.facilitator_org_id,
            facilitator_name=name,
            status=m.status,
            recommended_at=m.recommended_at,
            decided_at=m.decided_at,
            decided_by=m.decided_by,
            decision_reason_code=m.decision_reason_code,
            decision_reason_text=m.decision_reason_text,
        )
        for (m, people_id3, name, fpg_name, fpg_country) in rows
    ]
    return ContactMatchesResponse(items=items, total=total)


@router.get("/{contact_id}/transitions", response_model=ContactTransitionsResponse)
async def get_contact_transitions(
    contact_id: uuid.UUID,
    db: DbSession,
    _user: Annotated[tuple[object, frozenset[str]], Depends(_STAFF_DEP)],
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ContactTransitionsResponse:
    await _require_contact(db, contact_id)
    total = int(
        (
            await db.execute(
                select(func.count())
                .select_from(TransitionAudit)
                .where(TransitionAudit.contact_id == contact_id)
            )
        ).scalar_one()
    )
    rows = (
        await db.execute(
            select(TransitionAudit)
            .where(TransitionAudit.contact_id == contact_id)
            .order_by(TransitionAudit.occurred_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return ContactTransitionsResponse(
        items=[ContactTransitionRow.model_validate(r) for r in rows],
        total=total,
    )


@router.get("/{contact_id}/activity", response_model=ContactActivityResponse)
async def get_contact_activity(
    contact_id: uuid.UUID,
    db: DbSession,
    _user: Annotated[tuple[object, frozenset[str]], Depends(_STAFF_DEP)],
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ContactActivityResponse:
    await _require_contact(db, contact_id)
    total = int(
        (
            await db.execute(
                select(func.count())
                .select_from(ActivityLog)
                .where(ActivityLog.contact_id == contact_id)
            )
        ).scalar_one()
    )
    rows = (
        await db.execute(
            select(ActivityLog)
            .where(ActivityLog.contact_id == contact_id)
            .order_by(ActivityLog.occurred_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return ContactActivityResponse(
        items=[ContactActivityRow.model_validate(r) for r in rows],
        total=total,
    )


@router.get("/{contact_id}/enrollments", response_model=ContactEnrollmentsResponse)
async def get_contact_enrollments(
    contact_id: uuid.UUID,
    db: DbSession,
    _user: Annotated[tuple[object, frozenset[str]], Depends(_STAFF_DEP)],
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ContactEnrollmentsResponse:
    """Drip-campaign enrollments for the contact (the #55 read slice)."""
    await _require_contact(db, contact_id)
    total = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Enrollment)
                .where(Enrollment.contact_id == contact_id)
            )
        ).scalar_one()
    )
    rows = (
        await db.execute(
            select(Enrollment, Campaign.name)
            .join(Campaign, Campaign.id == Enrollment.campaign_id)
            .where(Enrollment.contact_id == contact_id)
            .order_by(Enrollment.enrolled_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    items = [
        ContactEnrollmentRow(
            id=e.id,
            campaign_id=e.campaign_id,
            campaign_name=name,
            state=e.state,
            current_step_position=e.current_step_position,
            enrolled_at=e.enrolled_at,
            last_step_sent_at=e.last_step_sent_at,
            exit_reason=e.exit_reason,
        )
        for (e, name) in rows
    ]
    return ContactEnrollmentsResponse(items=items, total=total)


@router.get("/{contact_id}/timeline", response_model=ContactTimelineResponse)
async def get_contact_timeline(
    contact_id: uuid.UUID,
    db: DbSession,
    _user: Annotated[tuple[object, frozenset[str]], Depends(_STAFF_DEP)],
    limit: int = Query(50, ge=1, le=200),
) -> ContactTimelineResponse:
    """Merged newest-first feed of transitions + matches + activity. Fetches
    up to ``limit`` of each source, merges in memory, and returns the top
    ``limit``. A future optimization can push the merge into SQL."""
    await _require_contact(db, contact_id)
    transitions = (
        await db.execute(
            select(TransitionAudit)
            .where(TransitionAudit.contact_id == contact_id)
            .order_by(TransitionAudit.occurred_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    activity = (
        await db.execute(
            select(ActivityLog)
            .where(ActivityLog.contact_id == contact_id)
            .order_by(ActivityLog.occurred_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    matches = (
        await db.execute(
            select(Match, FacilitatingOrg.name)
            .join(AdopterInterest, AdopterInterest.id == Match.adopter_interest_id)
            .join(FacilitatingOrg, FacilitatingOrg.id == Match.facilitator_org_id)
            .where(AdopterInterest.contact_id == contact_id)
            .order_by(Match.recommended_at.desc())
            .limit(limit)
        )
    ).all()

    entries: list[ContactTimelineEntry] = []
    for t in transitions:
        entries.append(
            ContactTimelineEntry(
                type="transition",
                at=t.occurred_at,
                title=f"{t.from_state or '—'} → {t.to_state}",
                detail=t.reason_text,
                ref_id=str(t.id),
            )
        )
    for a in activity:
        entries.append(
            ContactTimelineEntry(
                type="activity",
                at=a.occurred_at,
                title=a.kind or "note",
                detail=a.body[:280],
                ref_id=str(a.id),
            )
        )
    for m, name in matches:
        entries.append(
            ContactTimelineEntry(
                type="match",
                at=m.recommended_at,
                title=f"Match → {name} ({m.status})",
                detail=m.decision_reason_text,
                ref_id=str(m.id),
            )
        )
    entries.sort(key=lambda e: e.at, reverse=True)
    return ContactTimelineResponse(items=entries[:limit])


@router.post(
    "/{contact_id}/activity",
    response_model=ContactActivityRow,
    status_code=status.HTTP_201_CREATED,
)
async def add_contact_note(
    contact_id: uuid.UUID,
    body: ContactNoteCreate,
    db: DbSession,
    user_with_roles: Annotated[tuple[object, frozenset[str]], Depends(_STAFF_DEP)],
) -> ContactActivityRow:
    """Write a staff note into ``activity_log`` (kind defaults to ``note``).
    No outbox event — an internal note is not a domain state change."""
    await _require_contact(db, contact_id)
    user, _roles = user_with_roles
    note = ActivityLog(
        id=uuid.uuid4(),
        contact_id=contact_id,
        author_id=user.sub,
        body=body.body,
        kind=body.kind,
        source_system="local",
        occurred_at=datetime.now(UTC),
    )
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return ContactActivityRow.model_validate(note)


def _schedule_contact_email_send(
    background_tasks: BackgroundTasks,
    *,
    note_id: uuid.UUID,
    recipients: list[str],
    subject: str,
    body: str,
    reply_to: str | None,
    settings: SettingsDep,
) -> None:
    """Register the post-response ACS send (F3). The worker module is imported
    lazily so the API process does not import azure-communication-email at
    startup, mirroring ``auth_magic_link._enqueue_send_factory``."""
    try:
        from jp_adopt_worker.tasks.send_contact_email import (
            send_contact_email_inline,
        )
    except Exception:  # pragma: no cover - worker pkg optional in some envs
        logger.warning(
            "contact_email.enqueue.worker_pkg_unavailable note_id=%s", note_id
        )
        return
    background_tasks.add_task(
        send_contact_email_inline,
        note_id=note_id,
        recipients=recipients,
        subject=subject,
        body=body,
        reply_to=reply_to,
        acs_connection_string=settings.acs_connection_string,
        acs_sender_address=settings.acs_sender_address,
        database_url=settings.database_url,
    )


@router.post(
    "/{contact_id}/emails",
    response_model=ContactEmailResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_contact_email(
    contact_id: uuid.UUID,
    body: ContactEmailCreate,
    db: DbSession,
    settings: SettingsDep,
    background_tasks: BackgroundTasks,
    user_with_roles: Annotated[tuple[object, frozenset[str]], Depends(_STAFF_DEP)],
) -> ContactEmailResponse:
    """F3: email a contact and record the email as a note on the timeline.

    Matches the magic-link send pattern: write the note in-transaction,
    commit, then fire ``send_contact_email_inline`` via BackgroundTasks (ACS,
    dev-fallback log). The background task flips the note's stored
    ``source_metadata.status`` to ``sent`` / ``failed`` once delivery
    resolves. Inherits magic-link's known durability gap (a process crash
    between commit and send drops the email; the queued note row stays as a
    visible, re-sendable record)."""
    contact = await _require_contact(db, contact_id)
    user, _roles = user_with_roles
    if not contact.email_normalized:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "email_required",
                "message": "Contact has no email address to send to",
            },
        )
    recipients = [contact.email_normalized]
    # Secondary contact lives on the 1:1 contact_profile and is a
    # facilitator-only concept; ignore the flag for adopters or when no
    # secondary address is on file.
    if body.include_secondary and contact.party_kind == "facilitator":
        secondary = (
            await db.execute(
                select(ContactProfile.secondary_contact_email).where(
                    ContactProfile.contact_id == contact_id
                )
            )
        ).scalar_one_or_none()
        # Skip a secondary that duplicates the primary (common data-entry
        # case) so ACS isn't handed two identical 'to' addresses.
        if secondary and secondary != contact.email_normalized:
            recipients.append(secondary)

    note = ActivityLog(
        id=uuid.uuid4(),
        contact_id=contact_id,
        author_id=user.sub,
        body=body.body,
        kind="email",
        source_system="local",
        source_metadata={
            "subject": body.subject,
            "to": recipients,
            "status": "queued",
        },
        occurred_at=datetime.now(UTC),
    )
    db.add(note)
    await db.commit()
    await db.refresh(note)

    _schedule_contact_email_send(
        background_tasks,
        note_id=note.id,
        recipients=recipients,
        subject=body.subject,
        body=body.body,
        reply_to=getattr(user, "email", None),
        settings=settings,
    )
    return ContactEmailResponse(note_id=note.id, to=recipients, status="queued")


@router.put("/{contact_id}/assignment", response_model=ContactRead)
async def assign_contact(
    contact_id: uuid.UUID,
    body: ContactAssignmentRequest,
    db: DbSession,
    user_with_roles: Annotated[tuple[object, frozenset[str]], Depends(_STAFF_DEP)],
) -> ContactRead:
    """Assign the contact to a staff user (1:1; re-assigning replaces). Omitting
    ``user_subject_id`` assigns to the caller. Off the contacts row → no version
    bump."""
    contact = await _require_contact(db, contact_id)
    user, _roles = user_with_roles
    assignee = body.user_subject_id or user.sub
    asn = (
        await db.execute(
            select(ContactAssignment).where(
                ContactAssignment.contact_id == contact_id
            )
        )
    ).scalar_one_or_none()
    if asn is None:
        db.add(
            ContactAssignment(
                contact_id=contact.id,
                user_subject_id=assignee,
                assigned_by=user.sub,
            )
        )
    else:
        asn.user_subject_id = assignee
        asn.assigned_by = user.sub
        asn.assigned_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(contact)
    return await _contact_read_with_profile(db, contact)


@router.delete(
    "/{contact_id}/assignment", status_code=status.HTTP_204_NO_CONTENT
)
async def unassign_contact(
    contact_id: uuid.UUID,
    db: DbSession,
    _user: Annotated[tuple[object, frozenset[str]], Depends(_STAFF_DEP)],
) -> None:
    await _require_contact(db, contact_id)
    await db.execute(
        delete(ContactAssignment).where(
            ContactAssignment.contact_id == contact_id
        )
    )
    await db.commit()


@router.patch("/{contact_id}", response_model=ContactRead)
async def patch_contact(
    contact_id: uuid.UUID,
    body: ContactPatch,
    db: DbSession,
    _user: Annotated[tuple[object, frozenset[str]], Depends(_STAFF_DEP)],
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

    now = datetime.now(UTC)
    contact_changed = False
    if "party_kind" in updates:
        contact.party_kind = updates["party_kind"]
        contact_changed = True
    if "display_name" in updates:
        contact.display_name = updates["display_name"]
        contact_changed = True
    # adopter_status / facilitator_status are intentionally NOT patchable here
    # (F5): status mutations flow through POST /v1/contacts/{id}/transition so
    # role checks, reason codes, and the audit row are enforced.

    # U9: adoption-profile edits upsert the 1:1 contact_profile row. Kept off
    # the Contact row so they don't bump Contact.version (the optimistic-lock
    # column the match/transition flows gate on).
    if body.profile is not None:
        profile_updates = body.profile.model_dump(exclude_unset=True)
        # Don't materialize an empty contact_profile row on a no-op PATCH —
        # that would make a profile look "present" with no data behind it.
        if profile_updates:
            prof = (
                await db.execute(
                    select(ContactProfile).where(
                        ContactProfile.contact_id == contact.id
                    )
                )
            ).scalar_one_or_none()
            if prof is None:
                prof = ContactProfile(id=uuid.uuid4(), contact_id=contact.id)
                db.add(prof)
            for field_name, value in profile_updates.items():
                setattr(prof, field_name, value)

    # Only stamp + emit a contact.updated event when contact-level fields moved.
    if contact_changed:
        contact.updated_at = now
        emit_outbox(
            db,
            event_type=EVENT_CONTACT_UPDATED,
            payload={
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
            },
        )

    await db.commit()
    await db.refresh(contact)
    return await _contact_read_with_profile(db, contact)
