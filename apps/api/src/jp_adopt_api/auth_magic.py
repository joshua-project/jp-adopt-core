"""Magic-link side-car: issue + verify single-use email links and bridge
the resulting identity to ``identity_link``.

See ``docs/runbooks/magic-link-side-car.md`` for operational notes (TTL,
rate-limit window, anti-enumeration response shape, ACS dev fallback).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jp_adopt_api.auth import AuthUser
from jp_adopt_api.config import Settings
from jp_adopt_api.models import (
    IdentityLink,
    MagicLinkRateLimit,
    MagicLinkToken,
)

logger = logging.getLogger(__name__)

# Token TTL: deliberately short — magic-links are bearer secrets in email and
# should not be reusable. The plan locks this at 15 minutes.
MAGIC_LINK_TTL_SECONDS = 900

# Per-email rolling-hour throttle. Plan locks this at 6/hr/email.
MAGIC_LINK_RATE_LIMIT_PER_HOUR = 6
MAGIC_LINK_RATE_LIMIT_WINDOW = timedelta(hours=1)

# JWT lifetime after a successful claim — week one default is 7 days. The
# refresh story (refresh-token rotation) lives in U10.
MAGIC_LINK_JWT_TTL_SECONDS = 7 * 24 * 60 * 60

ANTI_ENUMERATION_MESSAGE = "If we have your email, we sent a link."


class RateLimitedError(Exception):
    """6+ requests for the same email in the last hour."""


class MagicLinkExpiredError(Exception):
    """Token row exists but ``expires_at`` has passed."""


class MagicLinkAlreadyClaimedError(Exception):
    """Token row exists but ``claimed_at`` is non-null."""


class MagicLinkInvalidError(Exception):
    """Token does not match any stored hash."""


class AccountResolutionConflictError(Exception):
    """An ``identity_link`` row exists for this email with a ``b2c_subject_id``
    that does not match the side-car identity we are about to bridge.

    The runbook documents the manual resolution procedure
    (`docs/runbooks/multi-idp-b2c.md` → ``account_resolution_conflict``).
    """


@dataclass(frozen=True)
class MagicLinkRequestResult:
    ok: bool
    message: str


@dataclass(frozen=True)
class ClaimResult:
    access_token: str
    token_type: str = "bearer"
    expires_in: int = MAGIC_LINK_JWT_TTL_SECONDS


def normalize_email(email: str) -> str:
    """Lower-cased, whitespace-trimmed, no trailing-dot email.

    Reused by every IdP integration so that ``identity_link.email_normalized``
    is consistent regardless of which side-car bridged the identity.
    """
    return email.strip().rstrip(".").lower()


def _hash_token(raw: str, signing_key: str) -> str:
    """SHA-256(raw_token || signing_key). The signing key adds a peppered
    domain separator so a leaked DB cannot replay magic-links against a
    different deployment.
    """
    h = hashlib.sha256()
    h.update(raw.encode("utf-8"))
    h.update(b"\x00")
    h.update(signing_key.encode("utf-8"))
    return h.hexdigest()


def generate_token(signing_key: str) -> tuple[str, str]:
    """Return ``(raw_token, token_hash)``. The raw token is what we email; the
    hash is what goes in the DB. We never store the raw token at rest.
    """
    raw = secrets.token_urlsafe(32)
    return raw, _hash_token(raw, signing_key)


async def _count_recent_requests(session: AsyncSession, email_normalized: str) -> int:
    cutoff = datetime.now(UTC) - MAGIC_LINK_RATE_LIMIT_WINDOW
    stmt = (
        select(func.count())
        .select_from(MagicLinkRateLimit)
        .where(
            MagicLinkRateLimit.email_normalized == email_normalized,
            MagicLinkRateLimit.requested_at >= cutoff,
        )
    )
    return int((await session.execute(stmt)).scalar_one())


async def request_magic_link(
    session: AsyncSession,
    *,
    email: str,
    ip: str | None,
    settings: Settings,
    enqueue: Any | None = None,
) -> MagicLinkRequestResult:
    """Generate + persist a magic-link token row; enqueue the email send.

    Returns an anti-enumeration response: identical shape whether the email
    is known or not. Rate-limit denial raises ``RateLimitedError`` (which
    the router translates to HTTP 429).
    """
    email_normalized = normalize_email(email)
    recent = await _count_recent_requests(session, email_normalized)
    if recent >= MAGIC_LINK_RATE_LIMIT_PER_HOUR:
        raise RateLimitedError(
            f"Rate limit: {MAGIC_LINK_RATE_LIMIT_PER_HOUR}/hour reached for "
            f"{email_normalized}"
        )

    raw, token_hash = generate_token(settings.magic_link_signing_key)
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=MAGIC_LINK_TTL_SECONDS)

    session.add(
        MagicLinkToken(
            id=uuid.uuid4(),
            email=email,
            email_normalized=email_normalized,
            token_hash=token_hash,
            expires_at=expires_at,
            requested_ip=ip,
            requested_at=now,
        )
    )
    session.add(
        MagicLinkRateLimit(
            id=uuid.uuid4(),
            email_normalized=email_normalized,
            requested_at=now,
        )
    )
    await session.flush()

    if enqueue is not None:
        # The router commits the DB write; only after commit do we hand the
        # send off to the worker. ``enqueue`` is the FastAPI BackgroundTasks
        # add_task callback (or a stub in tests).
        enqueue(
            email=email,
            raw_token=raw,
            click_url_base=settings.magic_link_click_base_url,
            acs_connection_string=settings.acs_connection_string,
            acs_sender_address=settings.acs_sender_address,
        )
    else:
        # No enqueue provided (e.g. tests): log the raw token to stdout so
        # the developer can complete the flow.
        logger.info(
            "magic_link.request email=%s click_url=%s/auth/claim?token=%s",
            email_normalized,
            settings.magic_link_click_base_url,
            raw,
        )

    return MagicLinkRequestResult(ok=True, message=ANTI_ENUMERATION_MESSAGE)


async def claim_magic_link(
    session: AsyncSession,
    *,
    raw_token: str,
    click_ip: str | None,
    user_agent: str | None,
    settings: Settings,
) -> ClaimResult:
    """Validate the raw token, mark it claimed, bridge identity_link, mint JWT."""
    token_hash = _hash_token(raw_token, settings.magic_link_signing_key)
    stmt = select(MagicLinkToken).where(MagicLinkToken.token_hash == token_hash)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise MagicLinkInvalidError("Unknown magic-link token")
    now = datetime.now(UTC)
    if row.claimed_at is not None:
        raise MagicLinkAlreadyClaimedError("Token already claimed")
    if row.expires_at <= now:
        raise MagicLinkExpiredError("Token expired")

    row.claimed_at = now
    row.claimed_ip = click_ip
    row.claimed_user_agent = user_agent

    email_normalized = row.email_normalized
    link_stmt = select(IdentityLink).where(
        IdentityLink.email_normalized == email_normalized
    )
    existing = (await session.execute(link_stmt)).scalars().first()

    if existing is None:
        identity = IdentityLink(
            id=uuid.uuid4(),
            email=row.email,
            email_normalized=email_normalized,
            idp_name="magic_link",
        )
        session.add(identity)
        await session.flush()
    else:
        # Account-resolution-conflict guard: if the existing identity is anchored
        # to a B2C subject, this magic-link sign-in must NOT silently bridge.
        # Surface an explicit conflict so an operator can resolve via the runbook.
        if existing.b2c_subject_id:
            raise AccountResolutionConflictError(
                "Email is already linked to a B2C identity; magic-link sign-in "
                "would create an ambiguous account binding."
            )
        identity = existing

    payload = {
        "iss": settings.magic_link_issuer,
        "sub": str(identity.id),
        "email": identity.email,
        "idp": "magic_link",
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + MAGIC_LINK_JWT_TTL_SECONDS,
    }
    token = jwt.encode(payload, settings.magic_link_signing_key, algorithm="HS256")
    return ClaimResult(access_token=token)


def decode_magic_link_token(token: str, settings: Settings) -> AuthUser:
    """Verify a magic-link HS256 JWT and return an ``AuthUser``.

    Differs from the B2C decoder: HS256 (symmetric, no JWKS), validates the
    constant magic-link issuer, and sets ``tid="magic_link"`` so downstream
    code can branch on identity provenance.
    """
    payload = jwt.decode(
        token,
        settings.magic_link_signing_key,
        algorithms=["HS256"],
        issuer=settings.magic_link_issuer,
        options={"verify_iss": True, "require": ["exp", "sub", "iat"]},
    )
    sub = str(payload["sub"])
    email = payload.get("email")
    return AuthUser(sub=sub, email=email, tid="magic_link")
