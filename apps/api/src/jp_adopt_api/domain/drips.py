"""Drip engine domain logic (U10).

Three concerns live here:

1. **Enrollment** — turning an outbox ``contact.*`` event into an
   :class:`Enrollment` row when a campaign's ``trigger_event_type``
   matches. Pinned to the campaign's current ``version`` so mid-flight
   edits don't change behavior for already-enrolled contacts.

2. **Step-due query** — given the current clock, which active
   enrollments have a step ready to send right now? The worker calls
   this every ~10s and claims rows with ``SELECT FOR UPDATE SKIP
   LOCKED`` so two worker ticks can't double-send.

3. **MJML render** — load the template file from disk, render with
   Jinja2 strict-undefined, return ``(subject, html, plain_text)``.
   Templates live in ``apps/api/email-templates/`` and are referenced by
   filename on :class:`CampaignStep`. Falls back to plain text when MJML
   tooling isn't installed (the v1 ship doesn't require the mjml CLI).

The router (``routers/drips.py``) handles CRUD; the worker
(``apps/worker/.../tasks/send_drip_step.py``) drives the send loop.
This module is the shared logic between them.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jp_adopt_api.email_utils import normalize_email
from jp_adopt_api.models import (
    Campaign,
    CampaignStep,
    Contact,
    Enrollment,
    EnrollmentEvent,
    SuppressionList,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Constants / errors
# ──────────────────────────────────────────────────────────────────────────


def _resolve_email_templates_dir() -> Path:
    """Locate ``email-templates/`` across the layouts this code runs in.

    In the source tree ``drips.py`` lives at
    ``apps/api/src/jp_adopt_api/domain/`` so the templates are 4 levels up.
    But the production containers install the package NON-editable
    (``uv sync --no-editable``), so at runtime ``__file__`` is
    ``.../site-packages/jp_adopt_api/domain/drips.py`` — there is no ``src``
    level and the 4-levels-up computation points at a nonexistent
    ``.../pythonX.Y/email-templates``. The Dockerfiles copy the templates to
    ``/app/apps/api/email-templates`` (the API WORKDIR is ``/app/apps/api``;
    the worker's is ``/app/apps/worker`` but it copies the dir to the same
    ``/app/apps/api`` path). Try the known locations and pick the first that
    exists. ``EMAIL_TEMPLATES_DIR`` env var overrides everything.
    """
    override = os.environ.get("EMAIL_TEMPLATES_DIR")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    candidates = [
        here.parents[3] / "email-templates",  # source tree: apps/api/email-templates
        Path("/app/apps/api/email-templates"),  # prod containers (API + worker)
        here.parents[1] / "email-templates",  # if ever shipped as package data
        Path.cwd() / "email-templates",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


# Location of the email templates (the branded shell + any file-based steps).
EMAIL_TEMPLATES_DIR = _resolve_email_templates_dir()


# Enrollment event types — append-only log entries. Adding a new
# event type is fine; renaming an existing one breaks replay.
EVENT_STEP_SENT = "step_sent"
EVENT_SEND_FAILED = "send_failed"
EVENT_SEND_FAILED_TEMPLATE_MISSING = "send_failed_template_missing"
EVENT_PAUSED = "paused"
EVENT_RESUMED = "resumed"
EVENT_EXITED = "exited"


# Exit reasons — recorded on Enrollment.exit_reason. Stable strings
# (downstream metrics group by them).
EXIT_REASON_COMPLETED = "completed"
EXIT_REASON_DO_NOT_ENGAGE = "do_not_engage"
EXIT_REASON_SUPPRESSED = "suppressed"
EXIT_REASON_BOUNCE_HARD = "bounce_hard"
EXIT_REASON_BOUNCE_SOFT_RETRIED = "bounce_soft_retried"
EXIT_REASON_TEMPLATE_MISSING = "template_missing"
EXIT_REASON_MANUAL = "manual"


class DripError(Exception):
    """Base class for drip-domain errors."""


class TemplateMissingError(DripError):
    """The MJML template file referenced by a step isn't on disk."""


# ──────────────────────────────────────────────────────────────────────────
# Suppression hash
# ──────────────────────────────────────────────────────────────────────────


def email_hash(email: str) -> str:
    """SHA-256 hex of the normalized email. Used as the
    ``suppression_list.email_hash`` primary key so the table stores no
    raw PII while still allowing the hot-path send-time check.
    """
    normalized = normalize_email(email)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def is_suppressed(session: AsyncSession, email: str) -> bool:
    """True iff ``email`` is on the suppression list."""
    if not email:
        return False
    h = email_hash(email)
    row = await session.execute(
        select(SuppressionList.email_hash).where(
            SuppressionList.email_hash == h
        )
    )
    return row.scalar_one_or_none() is not None


async def add_to_suppression_list(
    session: AsyncSession,
    *,
    email: str,
    reason: str,
    source_metadata: dict[str, Any] | None = None,
) -> SuppressionList:
    """Upsert into the suppression list. Re-suppressing the same email
    overwrites the prior ``reason`` / ``source_metadata`` with the new
    values; the row is returned in one round-trip via RETURNING."""
    insert_stmt = pg_insert(SuppressionList).values(
        email_hash=email_hash(email),
        reason=reason,
        source_metadata=source_metadata,
    )
    stmt = insert_stmt.on_conflict_do_update(
        index_elements=["email_hash"],
        set_={
            "reason": insert_stmt.excluded.reason,
            "source_metadata": insert_stmt.excluded.source_metadata,
        },
    ).returning(SuppressionList)
    result = await session.execute(stmt)
    return result.scalars().one()


# ──────────────────────────────────────────────────────────────────────────
# Enrollment
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class EnrollmentOutcome:
    enrollment_id: uuid.UUID | None
    # One of: created | already_enrolled | suppressed | do_not_engage |
    # no_campaign | no_contact
    reason: str


async def _find_active_campaigns_for_event(
    session: AsyncSession, event_type: str
) -> list[Campaign]:
    rows = await session.execute(
        select(Campaign).where(
            Campaign.status == "active",
            Campaign.trigger_event_type == event_type,
        )
    )
    return list(rows.scalars().all())


async def enroll_contact_in_campaign(
    session: AsyncSession,
    *,
    campaign: Campaign,
    contact: Contact,
) -> EnrollmentOutcome:
    """Insert an Enrollment for the (campaign, contact) pair when:
      * contact email is not on the suppression list
      * contact.adopter_status is not 'do_not_engage'
      * no open enrollment already exists for this pair

    Idempotent via the ``uq_enrollment_open_per_campaign_contact``
    partial unique index — concurrent triggers for the same contact
    converge to one row (the loser sees ``already_enrolled``).
    """
    if contact.adopter_status == "do_not_engage":
        return EnrollmentOutcome(None, "do_not_engage")
    contact_email = contact.email_normalized
    if contact_email and await is_suppressed(session, contact_email):
        return EnrollmentOutcome(None, "suppressed")

    # Start at the campaign's lowest step position rather than a hardcoded
    # 0. Campaigns whose authors deleted the early steps (or whose first
    # step lives at position=1) still get a sane starting point. The
    # activate endpoint already refuses campaigns with zero steps, so the
    # None branch should be unreachable in practice; fall back to 0 in
    # that case to preserve prior behavior.
    min_position = (
        await session.execute(
            select(CampaignStep.position)
            .where(CampaignStep.campaign_id == campaign.id)
            .order_by(CampaignStep.position.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    starting_position = min_position if min_position is not None else 0

    enrollment = Enrollment(
        id=uuid.uuid4(),
        campaign_id=campaign.id,
        contact_id=contact.id,
        campaign_version=campaign.version,
        current_step_position=starting_position,
        state="active",
    )
    try:
        async with session.begin_nested():
            session.add(enrollment)
            await session.flush()
    except IntegrityError:
        # Concurrent trigger already created the open enrollment.
        existing = await session.execute(
            select(Enrollment.id).where(
                Enrollment.campaign_id == campaign.id,
                Enrollment.contact_id == contact.id,
                Enrollment.state.in_(("pending", "active", "paused")),
            )
        )
        existing_id = existing.scalar_one_or_none()
        return EnrollmentOutcome(existing_id, "already_enrolled")

    logger.info(
        "drip.enrollment.created campaign=%s contact=%s enrollment=%s",
        campaign.id,
        contact.id,
        enrollment.id,
    )
    return EnrollmentOutcome(enrollment.id, "created")


async def enroll_on_event(
    session: AsyncSession,
    *,
    event_type: str,
    contact_id: uuid.UUID,
) -> list[EnrollmentOutcome]:
    """React to an outbox event. For every active campaign whose
    ``trigger_event_type`` matches, attempt to enroll the contact. The
    caller (worker drain) commits.
    """
    contact = await session.get(Contact, contact_id)
    if contact is None:
        return [EnrollmentOutcome(None, "no_contact")]
    campaigns = await _find_active_campaigns_for_event(session, event_type)
    if not campaigns:
        return [EnrollmentOutcome(None, "no_campaign")]
    outcomes: list[EnrollmentOutcome] = []
    for c in campaigns:
        outcome = await enroll_contact_in_campaign(
            session, campaign=c, contact=contact
        )
        outcomes.append(outcome)
    return outcomes


async def exit_enrollment(
    session: AsyncSession,
    enrollment: Enrollment,
    *,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Move an enrollment to ``exited`` state and append an
    EnrollmentEvent. Idempotent — re-exiting an already-exited row is a
    no-op."""
    if enrollment.state in ("completed", "exited"):
        return
    now = datetime.now(UTC)
    enrollment.state = "exited"
    enrollment.exited_at = now
    enrollment.exit_reason = reason
    session.add(
        EnrollmentEvent(
            enrollment_id=enrollment.id,
            event_type=EVENT_EXITED,
            payload={"reason": reason, **(metadata or {})},
        )
    )


async def exit_enrollments_for_contact(
    session: AsyncSession,
    *,
    contact_id: uuid.UUID,
    reason: str,
) -> int:
    """Exit every open (pending/active/paused) enrollment for the given
    contact. Returns the count. Used by the outbox consumer when a
    contact transitions to ``do_not_engage``."""
    rows = await session.execute(
        select(Enrollment).where(
            Enrollment.contact_id == contact_id,
            Enrollment.state.in_(("pending", "active", "paused")),
        )
    )
    enrollments = list(rows.scalars().all())
    for e in enrollments:
        await exit_enrollment(session, e, reason=reason)
    return len(enrollments)


# ──────────────────────────────────────────────────────────────────────────
# Step-due query
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class DueStep:
    enrollment: Enrollment
    step: CampaignStep
    campaign: Campaign
    contact: Contact


async def claim_due_steps(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 50,
) -> list[DueStep]:
    """Atomically claim up to ``limit`` enrollments whose next step is
    due. Uses ``SELECT … FOR UPDATE SKIP LOCKED`` so concurrent workers
    don't double-send.

    A step is "due" when:
      * enrollment.state = 'active'
      * the corresponding CampaignStep at current_step_position exists
      * ``coalesce(last_step_sent_at, enrolled_at) + delay_days <= now``

    The send-at-hour gate is NOT enforced here — the worker checks it
    after claiming and skips/releases the row if the local hour is
    outside the window. (Lock + recheck is cleaner than a single SQL
    expression involving timezone-aware now() per row.)
    """
    if now is None:
        now = datetime.now(UTC)

    # Walk step-position gaps: rather than requiring an exact equality on
    # ``position == current_step_position`` (which silently drops
    # enrollments whose step was deleted underneath them), find the
    # lowest-position step at or above the enrollment's current position
    # via a correlated scalar subquery. If a step exists exactly at
    # current_step_position the join behaves as before; if not, the next
    # step up is picked.
    next_step_id_subq = (
        select(CampaignStep.id)
        .where(
            CampaignStep.campaign_id == Enrollment.campaign_id,
            CampaignStep.position >= Enrollment.current_step_position,
        )
        .order_by(CampaignStep.position.asc())
        .limit(1)
        .correlate(Enrollment)
        .scalar_subquery()
    )

    rows = await session.execute(
        select(Enrollment, CampaignStep, Campaign, Contact)
        .join(Campaign, Campaign.id == Enrollment.campaign_id)
        .join(
            CampaignStep,
            CampaignStep.id == next_step_id_subq,
        )
        .join(Contact, Contact.id == Enrollment.contact_id)
        .where(
            Enrollment.state == "active",
            Campaign.status == "active",
            # Don't send to suppression-list contacts; the worker re-checks
            # via is_suppressed() after claim, but excluding them here
            # avoids burning a claim on rows we'd immediately abort.
            Contact.adopter_status != "do_not_engage",
            or_(
                Enrollment.last_step_sent_at.is_(None),
                Enrollment.last_step_sent_at
                + (CampaignStep.delay_days * timedelta(days=1))
                <= now,
            ),
        )
        .order_by(Enrollment.enrolled_at.asc())
        .limit(limit)
        .with_for_update(of=Enrollment, skip_locked=True)
    )
    out: list[DueStep] = []
    for enrollment, step, campaign, contact in rows.all():
        out.append(
            DueStep(
                enrollment=enrollment,
                step=step,
                campaign=campaign,
                contact=contact,
            )
        )
    return out


async def advance_enrollment(
    session: AsyncSession,
    enrollment: Enrollment,
    *,
    sent_at: datetime,
) -> bool:
    """Advance ``current_step_position`` to the next existing step and
    set ``last_step_sent_at``. Returns True if the enrollment advanced;
    False if no higher-position step remains and the enrollment is now
    ``completed``.

    Finds the next step via ``position > current`` (ordered, limit 1)
    rather than ``current + 1`` so deleted-step gaps don't strand
    in-flight enrollments. Authors editing a campaign can remove a
    middle step and existing enrollments will skip past the gap to the
    next-higher position on their next tick.
    """
    enrollment.last_step_sent_at = sent_at
    next_step_position = (
        await session.execute(
            select(CampaignStep.position)
            .where(
                CampaignStep.campaign_id == enrollment.campaign_id,
                CampaignStep.position > enrollment.current_step_position,
            )
            .order_by(CampaignStep.position.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if next_step_position is None:
        enrollment.state = "completed"
        enrollment.exited_at = sent_at
        enrollment.exit_reason = EXIT_REASON_COMPLETED
        return False
    enrollment.current_step_position = next_step_position
    return True


# ──────────────────────────────────────────────────────────────────────────
# Template render
# ──────────────────────────────────────────────────────────────────────────


# Name of the branded shell every step body is rendered into. Code-managed
# (logo / footer / unsubscribe); never editable from the in-app editor.
BASE_SHELL_TEMPLATE = "_base.html.jinja"


# Merge tokens an authored step body may reference. The in-app editor inserts
# these as atomic chips that serialize to literal ``{{ name }}``; the keys here
# are the single source of truth shared by the editor (via the API) and the
# render context (``build_step_context``). Adding a token means adding its key
# here AND to ``build_step_context``.
MERGE_TOKENS: tuple[tuple[str, str], ...] = (
    ("contact_display_name", "Recipient name"),
)


# Tags/attributes the authored body may keep. Matches the editor's toolbar
# exactly (headings, bold/italic, links, lists). Everything else is stripped.
_ALLOWED_TAGS = {
    "h1",
    "h2",
    "h3",
    "p",
    "strong",
    "b",
    "em",
    "i",
    "a",
    "ul",
    "ol",
    "li",
    "br",
}


def sanitize_body_html(raw: str) -> str:
    """Sanitize in-app-authored body HTML against a tight allowlist. Applied on
    SAVE (the DB is the trust boundary; render treats the body as trusted).

    ``{{ token }}`` placeholders are plain text, not markup, so nh3 leaves them
    untouched. Token substitution at render time is a plain text replace (see
    ``substitute_body_tokens``), so the body is never compiled as a template.

    Fails CLOSED: nh3 is a hard dependency, so an ImportError means the build is
    broken — we raise rather than silently persisting unsanitized HTML.
    """
    import nh3

    return nh3.clean(
        raw,
        tags=_ALLOWED_TAGS,
        attributes={"a": {"href", "title"}},
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer",
        # Drop the *content* of disallowed script/style, not just the tags.
        clean_content_tags={"script", "style"},
    )


def build_step_context(
    *,
    contact_display_name: str,
    contact_email: str,
    campaign_name: str,
    step_position: int,
) -> dict[str, Any]:
    """Single source of truth for the merge fields available to a step body.
    Used by the worker send path, the preview endpoint, and test-send so all
    three render with identical keys (a missing key under StrictUndefined would
    render fine in one path and raise in another)."""
    return {
        "contact_display_name": contact_display_name,
        "contact_email": contact_email,
        "campaign_name": campaign_name,
        "step_position": step_position,
    }


def _html_to_plain(html: str) -> str:
    """Derive a text/plain alternative from rendered HTML. Prefers html2text
    (keeps link URLs and list structure); falls back to a tag-strip regex if
    the library is unavailable so the worker never crashes."""
    try:
        import html2text

        h = html2text.HTML2Text()
        h.body_width = 0  # no hard wrapping
        h.ignore_emphasis = True  # avoid *bold* / _italic_ markdown artifacts
        h.ignore_images = True
        return h.handle(html).strip()
    except Exception:  # pragma: no cover - html2text optional
        plain = re.sub(r"<[^>]+>", "", html)
        return re.sub(r"\n\s*\n", "\n\n", plain).strip()


_MERGE_TOKEN_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def substitute_body_tokens(body_html: str, context: dict[str, Any]) -> str:
    """Substitute ``{{ token }}`` placeholders in an authored body with their
    HTML-escaped context values. Unknown tokens (not in ``context``) are left as
    literal text.

    Critically, this is a plain text substitution — the body is NEVER compiled
    as a Jinja template. In-app-authored bodies are attacker-influenced (staff
    can type anything, and nh3 sanitization does not strip ``{{ }}`` / ``{% %}``
    text), so feeding them to ``Environment.from_string`` would be a
    server-side-template-injection / RCE sink. Substituting only known tokens as
    escaped values keeps personalization working with no template evaluation.
    """
    from markupsafe import escape as _escape

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in context:
            return str(_escape(str(context[name])))
        return match.group(0)  # unknown token → inert literal text

    return _MERGE_TOKEN_RE.sub(_replace, body_html)


# Brand styles applied at render time to authored-body tags that lack their own
# inline style. Keeps editor-authored bodies (Tiptap emits bare tags) visually
# consistent with the seeded copy, and keeps a seeded step looking the same after
# its first edit strips the file's inline styles. Email clients ignore <style>
# blocks, so the styling must be inline. Colors match the _base shell + the
# original template copy (#2C474B headings, #374151 body, #eb5f1e accent links).
_BODY_TAG_STYLES: dict[str, str] = {
    "h1": "margin:0 0 16px;color:#2C474B;font-size:22px",
    "h2": "margin:0 0 16px;color:#2C474B;font-size:18px",
    "h3": "margin:0 0 12px;color:#2C474B;font-size:16px",
    "p": "margin:0 0 16px;color:#374151",
    "a": "color:#eb5f1e",
    # Explicit list-style-type so email clients that reset list styling
    # (notably Outlook) still render markers.
    "ul": "margin:0 0 16px;color:#374151;padding-left:24px;list-style-type:disc",
    "ol": "margin:0 0 16px;color:#374151;padding-left:24px;list-style-type:decimal",
    "li": "margin:0 0 8px;color:#374151",
}

_BODY_TAG_RE = re.compile(
    r"<(" + "|".join(_BODY_TAG_STYLES) + r")(\s[^>]*)?>",
    re.IGNORECASE,
)


def apply_body_styles(html: str) -> str:
    """Inject brand inline styles into body tags that don't already carry a
    ``style=`` attribute. Tags with an author/seed style are left untouched."""

    def _style(match: re.Match[str]) -> str:
        tag = match.group(1).lower()
        attrs = match.group(2) or ""
        if "style=" in attrs.lower():
            return match.group(0)
        return f'<{match.group(1)}{attrs} style="{_BODY_TAG_STYLES[tag]}">'

    return _BODY_TAG_RE.sub(_style, html)


def render_step_html(
    *,
    template_name: str | None = None,
    body_html: str | None = None,
    context: dict[str, Any],
    templates_dir: Path | None = None,
) -> tuple[str, str]:
    """Render an email step. Returns ``(html, plain_text)``.

    Content comes from one of two sources, ``body_html`` preferred:

    * ``body_html`` — in-app-authored rich-text body (sanitized on save). Its
      ``{{ token }}`` placeholders are substituted as escaped values (NOT
      compiled as a template — see ``substitute_body_tokens``), then the result
      is injected as data into the branded shell. The editor never touches the
      shell. This is the live path: edits reach the next send without versioning.
    * ``template_name`` — a trusted, code-managed file in ``email-templates/``
      (the legacy path, still used by the digest and any unmigrated step). These
      ARE rendered as Jinja templates because they are code, not user input.

    Plain-text is derived from the rendered HTML.
    """
    if not body_html and not template_name:
        raise ValueError("render_step_html requires body_html or template_name")

    base = templates_dir or EMAIL_TEMPLATES_DIR

    path = None
    if not body_html:
        path = base / template_name
        if not path.is_file():
            raise TemplateMissingError(
                f"Template not found: {path} "
                f"(searched {base}; ensure apps/api/email-templates/ ships "
                f"with the API container)"
            )

    # Auto-inject `current_year` for the branded footer (`_base.html.jinja`).
    # Callers shouldn't have to remember it; it never differs per send.
    import datetime as _dt

    full_context = {"current_year": _dt.datetime.now(_dt.UTC).year, **context}

    try:
        from jinja2 import (
            Environment,
            FileSystemLoader,
            StrictUndefined,
            select_autoescape,
        )
    except ImportError as e:  # pragma: no cover - jinja2 is a fastapi transitive dep
        # Hard fallback: jinja2 truly missing. Naive substitution so the worker
        # doesn't crash — but `{% extends %}` won't resolve, so the branded
        # chrome is lost. File templates lose StrictUndefined's loud failure too.
        logger.warning(
            "drip.render.jinja2_unavailable using_naive_substitution err=%s", e
        )
        if body_html:
            html = substitute_body_tokens(body_html, full_context)
        else:
            html = path.read_text(encoding="utf-8")
            for k, v in full_context.items():
                html = html.replace("{{ " + k + " }}", str(v))
                html = html.replace("{{" + k + "}}", str(v))
        return html, _html_to_plain(html)

    # FileSystemLoader lets templates resolve `{% extends "_base.html.jinja" %}`.
    env = Environment(
        loader=FileSystemLoader(str(base)),
        undefined=StrictUndefined,
        autoescape=select_autoescape(["html", "xml", "mjml"]),
    )
    if body_html:
        # Substitute tokens first (escaped values, no template eval), then inject
        # the result as DATA into a FIXED wrapper. The wrapper string is a
        # constant — body_html never reaches the template compiler, so there is
        # no SSTI surface. `| safe` keeps the already-sanitized body HTML intact;
        # token values were HTML-escaped during substitution.
        substituted = apply_body_styles(
            substitute_body_tokens(body_html, full_context)
        )
        wrapper = (
            "{% extends '" + BASE_SHELL_TEMPLATE + "' %}"
            "{% block body %}{{ body_content | safe }}{% endblock %}"
        )
        html = env.from_string(wrapper).render(
            body_content=substituted, **full_context
        )
    else:
        html = env.get_template(template_name).render(**full_context)

    return html, _html_to_plain(html)


# ──────────────────────────────────────────────────────────────────────────
# Event-log convenience
# ──────────────────────────────────────────────────────────────────────────


def log_enrollment_event(
    session: AsyncSession,
    enrollment_id: uuid.UUID,
    *,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append an EnrollmentEvent. Synchronous (just adds to session)."""
    session.add(
        EnrollmentEvent(
            enrollment_id=enrollment_id,
            event_type=event_type,
            payload=payload,
        )
    )


__all__ = [
    "EMAIL_TEMPLATES_DIR",
    "EVENT_EXITED",
    "EVENT_PAUSED",
    "EVENT_RESUMED",
    "EVENT_SEND_FAILED",
    "EVENT_SEND_FAILED_TEMPLATE_MISSING",
    "EVENT_STEP_SENT",
    "EXIT_REASON_BOUNCE_HARD",
    "EXIT_REASON_BOUNCE_SOFT_RETRIED",
    "EXIT_REASON_COMPLETED",
    "EXIT_REASON_DO_NOT_ENGAGE",
    "EXIT_REASON_MANUAL",
    "EXIT_REASON_SUPPRESSED",
    "EXIT_REASON_TEMPLATE_MISSING",
    "DripError",
    "DueStep",
    "EnrollmentOutcome",
    "TemplateMissingError",
    "add_to_suppression_list",
    "advance_enrollment",
    "claim_due_steps",
    "email_hash",
    "enroll_contact_in_campaign",
    "enroll_on_event",
    "exit_enrollment",
    "exit_enrollments_for_contact",
    "is_suppressed",
    "log_enrollment_event",
    "render_step_html",
]


# ``Iterable`` was imported for forward-compatible callers; not used in
# the public surface but listed here to silence linters.
_ = Iterable
