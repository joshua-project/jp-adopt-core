"""Magic-link request + claim endpoints (side-car).

Two POST endpoints — never GET — to avoid email-client prefetchers consuming
the single-use link before the user clicks. The /request endpoint always
returns 202 with the anti-enumeration shape (status code does not leak
whether the email exists in identity_link or contacts).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from pydantic import BaseModel, Field

from jp_adopt_api.auth_magic import (
    AccountResolutionConflictError,
    MagicLinkAlreadyClaimedError,
    MagicLinkExpiredError,
    MagicLinkInvalidError,
    RateLimitedError,
    claim_magic_link,
    request_magic_link,
)
from jp_adopt_api.deps import DbSession, SettingsDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/auth/magic-link", tags=["auth"])


class MagicLinkRequest(BaseModel):
    # We accept plain str (not EmailStr) to avoid the optional email-validator
    # dep; ``normalize_email`` already lower-cases/strips, and the simple "@"
    # check below is sufficient to reject obvious garbage. Anti-enumeration
    # demands we do not 422 on "not-an-email" — log and accept silently.
    email: str = Field(min_length=3, max_length=320)


class MagicLinkRequestResponse(BaseModel):
    ok: bool
    message: str


class MagicLinkClaim(BaseModel):
    token: str


class MagicLinkTokenEnvelope(BaseModel):
    access_token: str
    token_type: str
    expires_in: int


def _enqueue_send_factory(background_tasks: BackgroundTasks):
    """Return a callable that schedules the send_magic_link_email worker task.

    The worker module is imported lazily so the API process does not import
    azure-communication-email at startup (worker pkg owns that dependency).
    """

    def _enqueue(**kwargs):
        try:
            from jp_adopt_worker.tasks.send_magic_link_email import (
                send_magic_link_email_inline,
            )
        except Exception:  # pragma: no cover - worker pkg optional in some envs
            logger.warning(
                "magic_link.enqueue: jp_adopt_worker not importable; logging only"
            )
            logger.info("magic_link.email_skipped %s", kwargs)
            return
        background_tasks.add_task(send_magic_link_email_inline, **kwargs)

    return _enqueue


@router.post(
    "/request",
    response_model=MagicLinkRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_link(
    body: MagicLinkRequest,
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    background_tasks: BackgroundTasks,
) -> MagicLinkRequestResponse:
    ip = request.client.host if request.client else None
    email = body.email.strip()
    # Anti-enumeration: return identical 202 shape on obviously-malformed emails
    # so probers cannot infer whether email-validator (or our normalizer) rejected
    # the input. We log the rejection but do not surface it.
    if "@" not in email or "." not in email:
        logger.info("magic_link.request.malformed email_present=true")
        return MagicLinkRequestResponse(
            ok=True, message="If we have your email, we sent a link."
        )
    try:
        result = await request_magic_link(
            db,
            email=email,
            ip=ip,
            settings=settings,
            enqueue=_enqueue_send_factory(background_tasks),
        )
        await db.commit()
    except RateLimitedError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "rate_limited", "message": str(e)},
        ) from None
    return MagicLinkRequestResponse(ok=result.ok, message=result.message)


@router.post("/claim", response_model=MagicLinkTokenEnvelope)
async def claim_link(
    body: MagicLinkClaim,
    request: Request,
    db: DbSession,
    settings: SettingsDep,
) -> MagicLinkTokenEnvelope:
    ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    try:
        result = await claim_magic_link(
            db,
            raw_token=body.token,
            click_ip=ip,
            user_agent=user_agent,
            settings=settings,
        )
        await db.commit()
    except MagicLinkExpiredError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"code": "expired", "message": "Magic-link expired"},
        ) from None
    except MagicLinkAlreadyClaimedError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"code": "already_claimed", "message": "Magic-link already claimed"},
        ) from None
    except MagicLinkInvalidError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_token", "message": "Magic-link token is invalid"},
        ) from None
    except AccountResolutionConflictError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "account_resolution_conflict", "message": str(e)},
        ) from None
    return MagicLinkTokenEnvelope(
        access_token=result.access_token,
        token_type=result.token_type,
        expires_in=result.expires_in,
    )
