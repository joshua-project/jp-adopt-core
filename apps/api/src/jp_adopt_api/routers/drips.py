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
  * ``PATCH  /v1/drips/campaigns/{id}/steps/{position}`` — edit a step
  * ``DELETE /v1/drips/campaigns/{id}/steps/{position}`` — remove a step
  * ``POST   /v1/drips/campaigns/{id}/steps/{position}/preview`` — render preview
  * ``POST   /v1/drips/campaigns/{id}/steps/{position}/send-test`` — test send
  * ``GET    /v1/drips/merge-tokens``             — personalization tokens
  * ``POST   /v1/drips/campaigns/{id}/enroll``    — manual enroll a contact

Steps carry either in-app-authored ``body_html`` (rendered into the
code-managed branded shell) or a legacy ``mjml_template_name`` file; body
content is sanitized on save. Activation does NOT auto-enroll existing
contacts unless ``auto_enroll_existing=true`` on the campaign — even then the
bulk back-fill is a future endpoint, not this CRUD.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator
from sqlalchemy import delete, func, select

from jp_adopt_api.auth import AuthUser
from jp_adopt_api.config import get_settings
from jp_adopt_api.deps import STAFF_ROLES, DbSession, require_role
from jp_adopt_api.domain.drips import (
    EMAIL_TEMPLATES_DIR,
    EXIT_REASON_MANUAL,
    MERGE_TOKENS,
    TemplateMissingError,
    build_step_context,
    enroll_contact_in_campaign,
    render_step_html,
    sanitize_body_html,
)
from jp_adopt_api.models import (
    Campaign,
    CampaignStep,
    Contact,
    Enrollment,
    StaffProfile,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/drips", tags=["drips"])


# ──────────────────────────────────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────────────────────────────────


CampaignStatus = Literal["draft", "active", "paused", "archived"]
CampaignTriggerType = Literal["event", "manual"]


# Authored body content can be a few KB of HTML; cap it generously to bound
# the sanitize cost and reject pathological payloads.
_BODY_HTML_MAX = 50_000


class CampaignStepIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: int = Field(ge=0)
    delay_days: int = Field(ge=0, default=0)
    # A step carries EITHER inline body_html (in-app authored) OR a template
    # filename (legacy). At least one is required (validated below).
    mjml_template_name: str | None = Field(
        default=None, min_length=1, max_length=512
    )
    body_html: str | None = Field(default=None, max_length=_BODY_HTML_MAX)
    subject: str = Field(min_length=1, max_length=512)
    send_at_hour: int = Field(ge=0, le=23, default=9)
    send_at_minute: int = Field(ge=0, le=59, default=0)

    @model_validator(mode="after")
    def _require_content_source(self) -> CampaignStepIn:
        if not self.mjml_template_name and not self.body_html:
            raise ValueError(
                "a step requires either body_html or mjml_template_name"
            )
        return self


class CampaignStepPatch(BaseModel):
    """In-place edit of a step. All fields optional; only supplied
    fields are updated.

    Changing ``position`` to a value already occupied by another
    step is treated as a swap (transactional). This is how the UI's
    up/down reorder buttons work — they PATCH with the neighbor's
    position and let the server do the dance.
    """

    model_config = ConfigDict(extra="forbid")

    position: int | None = Field(default=None, ge=0)
    delay_days: int | None = Field(default=None, ge=0)
    mjml_template_name: str | None = Field(
        default=None, min_length=1, max_length=512
    )
    body_html: str | None = Field(default=None, max_length=_BODY_HTML_MAX)
    subject: str | None = Field(default=None, min_length=1, max_length=512)
    send_at_hour: int | None = Field(default=None, ge=0, le=23)
    send_at_minute: int | None = Field(default=None, ge=0, le=59)


class CampaignStepRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    campaign_id: uuid.UUID
    position: int
    delay_days: int
    mjml_template_name: str | None
    body_html: str | None
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


class StepPreviewResponse(BaseModel):
    """Rendered preview of a campaign step with sample context.

    The campaign id, step position, mjml_template_name, and subject are
    echoed back so a UI can display the metadata alongside the rendered
    HTML without round-tripping to the parent campaign endpoint.
    """

    campaign_id: uuid.UUID
    position: int
    mjml_template_name: str | None
    subject: str
    html: str
    plain: str
    sample_context: dict[str, str]


class MergeToken(BaseModel):
    """A personalization token the editor can insert as an atomic chip."""

    name: str  # the literal token, e.g. "contact_display_name"
    label: str  # friendly label shown in the picker, e.g. "Recipient name"


class MergeTokenListResponse(BaseModel):
    items: list[MergeToken]


class SendTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Optional override; defaults to the authenticated staff member's email.
    # EmailStr rejects a malformed address with 422 synchronously rather than
    # 202-then-silently-failing in the background send.
    to_email: EmailStr | None = Field(default=None)


class SendTestResponse(BaseModel):
    to_email: str


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
        # Sanitize on save — the DB is the trust boundary (see
        # domain.drips.sanitize_body_html). {{ }} tokens survive untouched.
        body_html=(
            sanitize_body_html(body.body_html)
            if body.body_html is not None
            else None
        ),
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


@router.patch(
    "/campaigns/{campaign_id}/steps/{position}",
    response_model=CampaignStepRead,
)
async def patch_step(
    campaign_id: uuid.UUID,
    position: int,
    body: CampaignStepPatch,
    db: DbSession,
    _: Annotated[tuple[object, frozenset[str]], Depends(_drips_dep)],
) -> CampaignStepRead:
    """Edit a step in place — subject, delay, template, send time, or
    position. Position change to an occupied slot swaps with the
    incumbent in a single transaction."""
    campaign = await _load_campaign(db, campaign_id)
    step = (
        await db.execute(
            select(CampaignStep).where(
                CampaignStep.campaign_id == campaign_id,
                CampaignStep.position == position,
            )
        )
    ).scalar_one_or_none()
    if step is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "step_not_found",
                "message": f"Campaign has no step at position {position}",
            },
        )
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        return CampaignStepRead.model_validate(step)

    # Position swap is the only field that needs special handling —
    # the unique (campaign_id, position) index forbids two rows in
    # the same slot. We park the swapped-with row at a temporary
    # negative position, move step into its slot, then move the
    # parked row to step's old slot. All in one transaction.
    new_position = updates.pop("position", None)
    if new_position is not None and new_position != step.position:
        other = (
            await db.execute(
                select(CampaignStep).where(
                    CampaignStep.campaign_id == campaign_id,
                    CampaignStep.position == new_position,
                )
            )
        ).scalar_one_or_none()
        old_position = step.position
        if other is not None:
            # Park `other` out of the way. -1 is illegal for CHECK; use
            # a large negative computed from the IDs to stay unique
            # across concurrent edits (the row is briefly invisible to
            # SELECTs filtered by position >= 0). Then commit the
            # swap. CHECK is `position >= 0` so we MUST flush an
            # intermediate value the constraint will accept... but
            # CHECK rejects negatives. Workaround: park at
            # max(position)+1 which is always >= 0 and won't collide.
            highest = (
                await db.execute(
                    select(func.max(CampaignStep.position)).where(
                        CampaignStep.campaign_id == campaign_id
                    )
                )
            ).scalar_one()
            park_at = (highest or 0) + 1
            other.position = park_at
            await db.flush()
            step.position = new_position
            await db.flush()
            other.position = old_position
            await db.flush()
        else:
            step.position = new_position
            await db.flush()

    # Sanitize authored body on save (DB is the trust boundary). An explicit
    # null clears the body (only valid when a template fallback remains).
    if updates.get("body_html") is not None:
        updates["body_html"] = sanitize_body_html(updates["body_html"])

    # Other field updates are straight setattr.
    for k, v in updates.items():
        setattr(step, k, v)

    # A step must always have at least one content source. CampaignStepIn
    # enforces this at create; enforce it here too so a PATCH that clears the
    # body of a body-only step (no template fallback) can't leave the step
    # uncontent-able and crash every future send.
    if not step.body_html and not step.mjml_template_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "no_content_source",
                "message": (
                    "A step needs body content or a template; cannot clear "
                    "the body of a step with no template fallback"
                ),
            },
        )

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
    "/campaigns/{campaign_id}/steps/{position}/preview",
    response_model=StepPreviewResponse,
    responses={
        404: {"description": "Campaign, step at position, or template not found"},
    },
)
async def preview_step(
    campaign_id: uuid.UUID,
    position: int,
    db: DbSession,
    _: Annotated[tuple[object, frozenset[str]], Depends(_drips_dep)],
) -> StepPreviewResponse:
    """Render a campaign step against sample context for UI preview.

    The sample contact is a stable, recognizable placeholder ("Alex
    Smith") so previews are deterministic across renders. No DB writes;
    no enrollment side effects.
    """
    campaign = await _load_campaign(db, campaign_id)
    step = (
        await db.execute(
            select(CampaignStep).where(
                CampaignStep.campaign_id == campaign_id,
                CampaignStep.position == position,
            )
        )
    ).scalar_one_or_none()
    if step is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "step_not_found",
                "message": f"Campaign has no step at position {position}",
            },
        )
    context = build_step_context(
        contact_display_name="Alex Smith",
        contact_email="alex.smith@example.com",
        campaign_name=campaign.name,
        step_position=step.position,
    )
    try:
        html, plain = render_step_html(
            template_name=step.mjml_template_name,
            body_html=step.body_html,
            context=context,
        )
    except TemplateMissingError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "template_not_found",
                "message": str(e),
            },
        ) from e
    return StepPreviewResponse(
        campaign_id=campaign_id,
        position=step.position,
        mjml_template_name=step.mjml_template_name,
        subject=step.subject,
        html=html,
        plain=plain,
        sample_context={k: str(v) for k, v in context.items()},
    )


@router.get("/merge-tokens", response_model=MergeTokenListResponse)
async def list_merge_tokens(
    _: Annotated[tuple[object, frozenset[str]], Depends(_drips_dep)],
) -> MergeTokenListResponse:
    """The personalization tokens the body editor may insert. Single source of
    truth (``domain.drips.MERGE_TOKENS``) shared with the render context so the
    picker can only emit tokens the renderer knows about."""
    return MergeTokenListResponse(
        items=[MergeToken(name=name, label=label) for name, label in MERGE_TOKENS]
    )


@router.post(
    "/campaigns/{campaign_id}/steps/{position}/send-test",
    response_model=SendTestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        400: {"description": "No recipient email could be determined"},
        404: {"description": "Campaign, step, or template not found"},
    },
)
async def send_test_step(
    campaign_id: uuid.UUID,
    position: int,
    body: SendTestRequest,
    db: DbSession,
    background_tasks: BackgroundTasks,
    actor: Annotated[tuple[AuthUser, frozenset[str]], Depends(_drips_dep)],
) -> SendTestResponse:
    """Render a step against sample context and send it to the caller (or an
    explicit ``to_email``) for a confidence check before activating. Reuses the
    worker ACS sender; not a contact state change, so no enrollment/outbox."""
    user, _roles = actor
    campaign = await _load_campaign(db, campaign_id)
    step = (
        await db.execute(
            select(CampaignStep).where(
                CampaignStep.campaign_id == campaign_id,
                CampaignStep.position == position,
            )
        )
    ).scalar_one_or_none()
    if step is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "step_not_found",
                "message": f"Campaign has no step at position {position}",
            },
        )

    to_email = body.to_email or user.email
    if not to_email:
        # Fall back to the staff profile's recorded email (token may omit it).
        to_email = (
            await db.execute(
                select(StaffProfile.email_normalized).where(
                    StaffProfile.b2c_subject_id == user.sub
                )
            )
        ).scalar_one_or_none()
    if not to_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "no_recipient",
                "message": "No to_email provided and no email on file for caller",
            },
        )

    context = build_step_context(
        contact_display_name="Alex Smith",
        contact_email=to_email,
        campaign_name=campaign.name,
        step_position=step.position,
    )
    try:
        html, plain = render_step_html(
            template_name=step.mjml_template_name,
            body_html=step.body_html,
            context=context,
        )
    except TemplateMissingError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "template_not_found", "message": str(e)},
        ) from e

    settings = get_settings()
    try:
        from jp_adopt_worker.tasks.send_drip_step import send_drip_test_inline
    except Exception as e:  # pragma: no cover - worker pkg optional in some envs
        logger.warning("drip.test_send.worker_pkg_unavailable err=%s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "worker_unavailable", "message": "Send unavailable"},
        ) from e

    background_tasks.add_task(
        send_drip_test_inline,
        to_email=to_email,
        subject=step.subject,
        html=html,
        plain=plain,
        acs_connection_string=settings.acs_connection_string,
        acs_sender_address=settings.acs_sender_address,
    )
    return SendTestResponse(to_email=to_email)


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
