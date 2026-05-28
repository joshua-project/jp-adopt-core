"""Daily digest domain logic (U11).

The worker calls :func:`build_digest_for_window` once per day at 9am ET.
That function:

  1. Queries :class:`Match` rows transitioned to ``recommended`` /
     ``accepted`` within ``[window_start, window_end)``.
  2. Groups matches by recipient: one "all matches today" digest to
     staff (Amy + adoption_manager addresses); one per-facilitator-org
     digest containing only their org's matches.
  3. Renders the ``daily-digest.mjml`` template per recipient with the
     match list as context.
  4. Returns a list of :class:`DigestRecipientPlan` objects the worker
     dispatches to ACS.

This module is pure-ish — it executes DB queries but never sends.
The worker owns ACS dispatch + the audit-row updates.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jp_adopt_api.domain.drips import (
    EMAIL_TEMPLATES_DIR,
    TemplateMissingError,
    render_step_html,
)
from jp_adopt_api.models import (
    AdopterInterest,
    Contact,
    FacilitatingOrg,
    FacilitatorOrgMembership,
    Match,
    Role,
    UserRole,
)

logger = logging.getLogger(__name__)


# Match statuses that count toward "today's matches" for the digest.
DIGEST_MATCH_STATUSES = ("recommended", "accepted")

# Role names whose holders receive the "all matches" digest.
STAFF_DIGEST_ROLE_NAMES = ("staff_admin", "adoption_manager")


# ──────────────────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class DigestMatch:
    """One match-row's worth of data ready for the email template."""

    match_id: uuid.UUID
    contact_id: uuid.UUID
    contact_display_name: str
    contact_email_normalized: str | None
    people_id3: str | None
    facilitator_org_id: uuid.UUID
    facilitator_name: str
    status: str
    recommended_at: datetime


@dataclass
class DigestRecipientPlan:
    """One pending send. The worker uses this to render + ship + audit."""

    recipient_address: str
    recipient_kind: str  # all_staff | adoption_manager | facilitator
    facilitator_org_id: uuid.UUID | None
    matches: list[DigestMatch] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────
# Query helpers
# ──────────────────────────────────────────────────────────────────────────


async def _load_matches_in_window(
    session: AsyncSession,
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[DigestMatch]:
    """Pull every match recommended/accepted in the window, joined to
    contact + facilitator org so the renderer has everything it needs
    without a second query per row."""
    rows = await session.execute(
        select(Match, Contact, FacilitatingOrg, AdopterInterest.people_id3)
        .join(
            AdopterInterest, AdopterInterest.id == Match.adopter_interest_id
        )
        .join(Contact, Contact.id == AdopterInterest.contact_id)
        .join(FacilitatingOrg, FacilitatingOrg.id == Match.facilitator_org_id)
        .where(
            Match.status.in_(DIGEST_MATCH_STATUSES),
            and_(
                Match.recommended_at >= window_start,
                Match.recommended_at < window_end,
            ),
        )
        .order_by(Match.recommended_at.asc())
    )
    out: list[DigestMatch] = []
    for match, contact, org, people_id3 in rows.all():
        out.append(
            DigestMatch(
                match_id=match.id,
                contact_id=contact.id,
                contact_display_name=contact.display_name,
                contact_email_normalized=contact.email_normalized,
                people_id3=people_id3,
                facilitator_org_id=org.id,
                facilitator_name=org.name,
                status=match.status,
                recommended_at=match.recommended_at,
            )
        )
    return out


async def _load_staff_recipients(
    session: AsyncSession,
) -> list[tuple[str, str]]:
    """Return ``(address, kind)`` tuples for every staff member whose
    role is in :data:`STAFF_DIGEST_ROLE_NAMES`. Address comes from the
    Contact row that shares the user's ``b2c_subject_id``; staff who
    have no Contact row are silently skipped.

    "kind" is ``all_staff`` for staff_admin and ``adoption_manager`` for
    the latter — both receive the same all-matches digest body in v1,
    but distinguishing them makes audit + future per-role templating
    cheaper."""
    rows = (
        await session.execute(
            select(Role.name, UserRole.user_subject_id)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(Role.name.in_(STAFF_DIGEST_ROLE_NAMES))
        )
    ).all()
    if not rows:
        return []
    subs = {sub for _, sub in rows}
    role_by_sub: dict[str, str] = {sub: name for name, sub in rows}

    contact_rows = (
        await session.execute(
            select(Contact.b2c_subject_id, Contact.email_normalized).where(
                Contact.b2c_subject_id.in_(subs)
            )
        )
    ).all()
    out: list[tuple[str, str]] = []
    for sub, email in contact_rows:
        if not email:
            continue
        role_name = role_by_sub.get(sub)
        kind = "all_staff" if role_name == "staff_admin" else "adoption_manager"
        out.append((email, kind))
    return out


async def _load_facilitator_recipients(
    session: AsyncSession,
    *,
    org_ids: set[uuid.UUID],
) -> dict[uuid.UUID, list[str]]:
    """Return ``{facilitator_org_id: [email, ...]}`` for every B2C user
    whose ``facilitator_org_membership`` covers one of ``org_ids`` AND
    who has a Contact row exposing a normalized email."""
    if not org_ids:
        return {}
    membership_rows = (
        await session.execute(
            select(
                FacilitatorOrgMembership.facilitator_org_id,
                FacilitatorOrgMembership.user_subject_id,
            ).where(FacilitatorOrgMembership.facilitator_org_id.in_(org_ids))
        )
    ).all()
    if not membership_rows:
        return {}
    subs = {sub for _, sub in membership_rows}
    contact_rows = (
        await session.execute(
            select(Contact.b2c_subject_id, Contact.email_normalized).where(
                Contact.b2c_subject_id.in_(subs)
            )
        )
    ).all()
    email_by_sub = {sub: email for sub, email in contact_rows if email}
    out: dict[uuid.UUID, list[str]] = {oid: [] for oid in org_ids}
    for org_id, sub in membership_rows:
        email = email_by_sub.get(sub)
        if email:
            out.setdefault(org_id, []).append(email)
    return {k: v for k, v in out.items() if v}


# ──────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────


async def build_digest_for_window(
    session: AsyncSession,
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[DigestRecipientPlan]:
    """Compute every digest recipient + their match list for the given
    window. Returns one :class:`DigestRecipientPlan` per recipient
    address. Empty list when no matches recommended/accepted in the
    window.
    """
    matches = await _load_matches_in_window(
        session, window_start=window_start, window_end=window_end
    )
    if not matches:
        return []

    # Staff: all matches.
    staff = await _load_staff_recipients(session)
    plans: dict[str, DigestRecipientPlan] = {}
    for address, kind in staff:
        plans[address] = DigestRecipientPlan(
            recipient_address=address,
            recipient_kind=kind,
            facilitator_org_id=None,
            matches=list(matches),
        )

    # Facilitators: only their org's matches.
    facilitator_orgs = {m.facilitator_org_id for m in matches}
    fac_recipients = await _load_facilitator_recipients(
        session, org_ids=facilitator_orgs
    )
    for org_id, emails in fac_recipients.items():
        org_matches = [m for m in matches if m.facilitator_org_id == org_id]
        if not org_matches:
            continue
        for address in emails:
            # If a facilitator user is also a staff member, the staff
            # digest already covers them — skip the duplicate. The plan
            # explicitly states staff get one digest covering everything;
            # facilitators receive a separate per-org digest only when
            # they aren't also staff.
            if address in plans:
                continue
            plans[address] = DigestRecipientPlan(
                recipient_address=address,
                recipient_kind="facilitator",
                facilitator_org_id=org_id,
                matches=org_matches,
            )

    return sorted(plans.values(), key=lambda p: p.recipient_address)


def render_digest_html(
    *,
    plan: DigestRecipientPlan,
    templates_dir: Path | None = None,
) -> tuple[str, str]:
    """Render the digest HTML + plain text for one recipient.

    Template name: ``daily-digest.mjml``. Available Jinja2 variables:
      * ``recipient_kind`` — frames the email ("Today's matches" vs
        "Your org's matches today")
      * ``match_count`` — int
      * ``matches`` — list of :class:`DigestMatch` (rendered as dicts)
    """
    context = {
        "recipient_kind": plan.recipient_kind,
        "match_count": len(plan.matches),
        "matches": [
            {
                "contact_display_name": m.contact_display_name,
                "people_id3": m.people_id3 or "",
                "facilitator_name": m.facilitator_name,
                "status": m.status,
                "recommended_at": m.recommended_at.isoformat(),
            }
            for m in plan.matches
        ],
    }
    try:
        return render_step_html(
            template_name="daily-digest.mjml",
            context=context,
            templates_dir=templates_dir,
        )
    except TemplateMissingError:
        # Re-raise with a digest-specific message so the operator knows
        # which file to add.
        raise TemplateMissingError(
            "Template apps/api/email-templates/daily-digest.mjml missing; "
            "see docs/runbooks/daily-digest.md for the placeholder."
        ) from None


__all__ = [
    "DIGEST_MATCH_STATUSES",
    "DigestMatch",
    "DigestRecipientPlan",
    "STAFF_DIGEST_ROLE_NAMES",
    "build_digest_for_window",
    "render_digest_html",
]


# Suppress unused-import: EMAIL_TEMPLATES_DIR is the documented default
# template directory; re-exported here so callers don't have to reach
# into the drips module for the path.
_ = (EMAIL_TEMPLATES_DIR, Any)
