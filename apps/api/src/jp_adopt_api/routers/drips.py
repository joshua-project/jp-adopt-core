"""Drip campaign CRUD router (U10).

Endpoints (staff-only):
  * ``GET    /v1/drips/campaigns``                — list campaigns
  * ``POST   /v1/drips/campaigns``                — create campaign
  * ``GET    /v1/drips/campaigns/{id}``           — single campaign + steps
  * ``PATCH  /v1/drips/campaigns/{id}``           — update meta fields
  * ``DELETE /v1/drips/campaigns/{id}``           — archive (soft delete)
  * ``POST   /v1/drips/campaigns/{id}/activate``  — flip status → active
  * ``POST   /v1/drips/campaigns/{id}/pause``     — flip status → paused
  * ``POST   /v1/drips/campaigns/{id}/steps``     — add a step
  * ``DELETE /v1/drips/campaigns/{id}/steps/{position}`` — remove a step
  * ``POST   /v1/drips/campaigns/{id}/enroll``    — manual enroll a contact

No preview / send-test endpoints in week-1 per the plan; authoring UI
defers to v2. Activation does NOT auto-enroll existing contacts unless
``auto_enroll_existing=true`` on the campaign — even then the bulk
back-fill is a future endpoint, not this CRUD.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select

from jp_adopt_api.deps import STAFF_ROLES, DbSession, require_role
from jp_adopt_api.domain.drips import (
    EMAIL_TEMPLATES_DIR,
    EXIT_REASON_MANUAL,
    enroll_contact_in_campaign,
)
from jp_adopt_api.models import (
    Campaign,
    CampaignStep,
    Contact,
    Enrollment,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/drips", tags=["drips"])


# ──────────────────────────────────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────────────────────────────────


CampaignStatus = Literal["draft", "active", "paused", "archived"]
CampaignTriggerType = Literal["event", "manual"]


class CampaignStepIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: int = Field(ge=0)
    delay_days: int = Field(ge=0, default=0)
    mjml_template_name: str = Field(min_length=1, max_length=512)
    subject: str = Field(min_length=1, max_length=512)
    send_at_hour: int = Field(ge=0, le=23, default=9)
    send_at_minute: int = Field(ge=0, le=59, default=0)


class CampaignStepRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    campaign_id: uuid.UUID
    position: int
    delay_days: int
    mjml_template_name: str
    subject: str
    send_at_hour: int
    send_at_minute: int
    created_at: datetime


class CampaignCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=512)
    description: str | None = Field(default=None, max_length=4096)
    trigger_type: CampaignTriggerType = "event"
    trigger_event_type: str | None = Field(default=None, max_length=256)
    auto_enroll_existing: bool = False
    precedence: int = 0


class CampaignPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=512)
    description: str | None = Field(default=None, max_length=4096)
    trigger_event_type: str | None = Field(default=None, max_length=256)
    auto_enroll_existing: bool | None = None
    precedence: int | None = None


class CampaignRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    status: str
    trigger_type: str
    trigger_event_type: str | None
    auto_enroll_existing: bool
    precedence: int
    version: int
    created_at: datetime
    updated_at: datetime
    steps: list[CampaignStepRead] = Field(default_factory=list)


class CampaignListResponse(BaseModel):
    items: list[CampaignRead]
    total: int


class TemplateRead(BaseModel):
    name: str


class TemplateListResponse(BaseModel):
    items: list[TemplateRead]


class ManualEnrollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: uuid.UUID


class ManualEnrollResponse(BaseModel):
    enrollment_id: uuid.UUID | None
    reason: str


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


_drips_dep = require_role(*STAFF_ROLES)


async def _load_campaign(db, campaign_id: uuid.UUID) -> Campaign:
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found"
        )
    return campaign


async def _serialize_campaign(db, campaign: Campaign) -> CampaignRead:
    steps = (
        await db.execute(
            select(CampaignStep)
            .where(CampaignStep.campaign_id == campaign.id)
            .order_by(CampaignStep.position.asc())
        )
    ).scalars().all()
    return CampaignRead(
        id=campaign.id,
        name=campaign.name,
        description=campaign.description,
        status=campaign.status,
        trigger_type=campaign.trigger_type,
        trigger_event_type=campaign.trigger_event_type,
        auto_enroll_existing=campaign.auto_enroll_existing,
        precedence=campaign.precedence,
        version=campaign.version,
        created_at=campaign.created_at,
        updated_at=campaign.updated_at,
        steps=[CampaignStepRead.model_validate(s) for s in steps],
    )


def _bump_version_if_published(campaign: Campaign) -> None:
    """Editing an active or paused campaign bumps its version so existing
    enrollments stay pinned to the version they started under. Draft
    campaigns are still being authored; no need to bump."""
    if campaign.status in ("active", "paused"):
        campaign.version += 1


# ──────────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────────


@router.get("/campaigns", response_model=CampaignListResponse)
async def list_campaigns(
    db: DbSession,
    _: Annotated[tuple[object, frozenset[str]], Depends(_drips_dep)],
) -> CampaignListResponse:
    rows = (
        await db.execute(
            select(Campaign).order_by(Campaign.created_at.desc())
        )
    ).scalars().all()
    # Batched step fetch: one SELECT for all campaigns instead of N+1.
    campaign_ids = [c.id for c in rows]
    steps_by_campaign: dict[uuid.UUID, list[CampaignStepRead]] = {
        cid: [] for cid in campaign_ids
    }
    if campaign_ids:
        step_rows = (
            await db.execute(
                select(CampaignStep)
                .where(CampaignStep.campaign_id.in_(campaign_ids))
                .order_by(
                    CampaignStep.campaign_id,
                    CampaignStep.position.asc(),
                )
            )
        ).scalars().all()
        for s in step_rows:
            steps_by_campaign[s.campaign_id].append(
                CampaignStepRead.model_validate(s)
            )
    items = [
        CampaignRead(
            id=c.id,
            name=c.name,
            description=c.description,
            status=c.status,
            trigger_type=c.trigger_type,
            trigger_event_type=c.trigger_event_type,
            auto_enroll_existing=c.auto_enroll_existing,
            precedence=c.precedence,
            version=c.version,
            created_at=c.created_at,
            updated_at=c.updated_at,
            steps=steps_by_campaign.get(c.id, []),
        )
        for c in rows
    ]
    return CampaignListResponse(items=items, total=len(items))


@router.get("/templates", response_model=TemplateListResponse)
async def list_templates(
    _: Annotated[tuple[object, frozenset[str]], Depends(_drips_dep)],
) -> TemplateListResponse:
    """Enumerate ``*.mjml`` files in ``EMAIL_TEMPLATES_DIR`` so the add-step
    UI can present a dropdown instead of a free-text field, eliminating
    typo-as-silent-send-failure. Returns ``{ items: [] }`` (200) if the
    directory is missing — Path.glob on a missing directory yields an
    empty iterator on Python 3.10+, so no try/except is needed."""
    names = sorted(p.name for p in EMAIL_TEMPLATES_DIR.glob("*.mjml"))
    return TemplateListResponse(items=[TemplateRead(name=n) for n in names])


@router.post(
    "/campaigns",
    response_model=CampaignRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_campaign(
    body: CampaignCreate,
    db: DbSession,
    _: Annotated[tuple[object, frozenset[str]], Depends(_drips_dep)],
) -> CampaignRead:
    if body.trigger_type == "event" and not body.trigger_event_type:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "trigger_event_type_required",
                "message": (
                    "trigger_type='event' requires trigger_event_type"
                ),
            },
        )
    campaign = Campaign(
        id=uuid.uuid4(),
        name=body.name,
        description=body.description,
        status="draft",
        trigger_type=body.trigger_type,
        trigger_event_type=body.trigger_event_type,
        auto_enroll_existing=body.auto_enroll_existing,
        precedence=body.precedence,
        version=1,
    )
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    return await _serialize_campaign(db, campaign)


@router.get("/campaigns/{campaign_id}", response_model=CampaignRead)
async def get_campaign(
    campaign_id: uuid.UUID,
    db: DbSession,
    _: Annotated[tuple[object, frozenset[str]], Depends(_drips_dep)],
) -> CampaignRead:
    campaign = await _load_campaign(db, campaign_id)
    return await _serialize_campaign(db, campaign)


@router.patch("/campaigns/{campaign_id}", response_model=CampaignRead)
async def patch_campaign(
    campaign_id: uuid.UUID,
    body: CampaignPatch,
    db: DbSession,
    _: Annotated[tuple[object, frozenset[str]], Depends(_drips_dep)],
) -> CampaignRead:
    campaign = await _load_campaign(db, campaign_id)
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "no_fields", "message": "No mutable fields"},
        )
    for k, v in updates.items():
        setattr(campaign, k, v)
    _bump_version_if_published(campaign)
    campaign.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(campaign)
    return await _serialize_campaign(db, campaign)


@router.delete(
    "/campaigns/{campaign_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def archive_campaign(
    campaign_id: uuid.UUID,
    db: DbSession,
    _: Annotated[tuple[object, frozenset[str]], Depends(_drips_dep)],
) -> None:
    """Soft delete: flip status to ``archived`` so audit history stays
    intact. Hard delete is reserved for failed-test cleanup.

    Refuses with 409 when active enrollments still reference the
    campaign — archiving mid-flight would orphan those enrollments
    relative to the worker's ``Campaign.status == 'active'`` filter.
    Operators should pause + manually exit (or wait for completion)
    before archiving.
    """
    campaign = await _load_campaign(db, campaign_id)
    active_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Enrollment)
                .where(
                    Enrollment.campaign_id == campaign_id,
                    Enrollment.state == "active",
                )
            )
        ).scalar_one()
    )
    if active_count > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "campaign_has_active_enrollments",
                "message": (
                    "Pause and let enrollments complete (or manually "
                    "exit them) before archiving"
                ),
                "active_count": active_count,
            },
        )
    campaign.status = "archived"
    campaign.updated_at = datetime.now(UTC)
    await db.commit()
    return None


@router.post("/campaigns/{campaign_id}/activate", response_model=CampaignRead)
async def activate_campaign(
    campaign_id: uuid.UUID,
    db: DbSession,
    _: Annotated[tuple[object, frozenset[str]], Depends(_drips_dep)],
) -> CampaignRead:
    campaign = await _load_campaign(db, campaign_id)
    if campaign.status == "archived":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "archived",
                "message": "Cannot activate an archived campaign",
            },
        )
    if campaign.status == "active":
        return await _serialize_campaign(db, campaign)
    has_steps = (
        await db.execute(
            select(CampaignStep.id)
            .where(CampaignStep.campaign_id == campaign.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if has_steps is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "no_steps",
                "message": "Campaign has no steps; add at least one",
            },
        )
    campaign.status = "active"
    campaign.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(campaign)
    return await _serialize_campaign(db, campaign)


@router.post("/campaigns/{campaign_id}/pause", response_model=CampaignRead)
async def pause_campaign(
    campaign_id: uuid.UUID,
    db: DbSession,
    _: Annotated[tuple[object, frozenset[str]], Depends(_drips_dep)],
) -> CampaignRead:
    campaign = await _load_campaign(db, campaign_id)
    if campaign.status == "archived":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "archived",
                "message": "Cannot pause an archived campaign",
            },
        )
    campaign.status = "paused"
    campaign.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(campaign)
    return await _serialize_campaign(db, campaign)


@router.post(
    "/campaigns/{campaign_id}/steps",
    response_model=CampaignStepRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_step(
    campaign_id: uuid.UUID,
    body: CampaignStepIn,
    db: DbSession,
    _: Annotated[tuple[object, frozenset[str]], Depends(_drips_dep)],
) -> CampaignStepRead:
    campaign = await _load_campaign(db, campaign_id)
    # Reject duplicate positions via the unique partial index, but check
    # explicitly first so the 409 carries a clear code.
    existing = (
        await db.execute(
            select(CampaignStep.id).where(
                CampaignStep.campaign_id == campaign_id,
                CampaignStep.position == body.position,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "position_in_use",
                "message": (
                    f"Step at position {body.position} already exists; "
                    "use PATCH to update it"
                ),
            },
        )
    step = CampaignStep(
        id=uuid.uuid4(),
        campaign_id=campaign_id,
        position=body.position,
        delay_days=body.delay_days,
        mjml_template_name=body.mjml_template_name,
        subject=body.subject,
        send_at_hour=body.send_at_hour,
        send_at_minute=body.send_at_minute,
    )
    db.add(step)
    _bump_version_if_published(campaign)
    campaign.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(step)
    return CampaignStepRead.model_validate(step)


@router.delete(
    "/campaigns/{campaign_id}/steps/{position}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_step(
    campaign_id: uuid.UUID,
    position: int,
    db: DbSession,
    _: Annotated[tuple[object, frozenset[str]], Depends(_drips_dep)],
) -> None:
    campaign = await _load_campaign(db, campaign_id)
    result = await db.execute(
        delete(CampaignStep).where(
            CampaignStep.campaign_id == campaign_id,
            CampaignStep.position == position,
        )
    )
    if result.rowcount == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Step not found at that position",
        )
    _bump_version_if_published(campaign)
    campaign.updated_at = datetime.now(UTC)
    await db.commit()
    return None


@router.post(
    "/campaigns/{campaign_id}/enroll",
    response_model=ManualEnrollResponse,
)
async def manual_enroll(
    campaign_id: uuid.UUID,
    body: ManualEnrollRequest,
    db: DbSession,
    _: Annotated[tuple[object, frozenset[str]], Depends(_drips_dep)],
) -> ManualEnrollResponse:
    campaign = await _load_campaign(db, campaign_id)
    if campaign.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "campaign_inactive",
                "message": (
                    "Campaign must be active to accept manual enrollments; "
                    f"current status is {campaign.status!r}"
                ),
            },
        )
    contact = await db.get(Contact, body.contact_id)
    if contact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found"
        )
    outcome = await enroll_contact_in_campaign(
        db, campaign=campaign, contact=contact
    )
    await db.commit()
    return ManualEnrollResponse(
        enrollment_id=outcome.enrollment_id, reason=outcome.reason
    )


# EXIT_REASON_MANUAL is used by the worker's drain to exit enrollments
# on do_not_engage; the import here is for re-export so external callers
# (and downstream linters) see a stable symbol path.
_ = EXIT_REASON_MANUAL
