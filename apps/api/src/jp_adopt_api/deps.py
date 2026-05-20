from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jp_adopt_api.auth import (
    AuthUser,
    DevelopmentAuthForbiddenError,
    authenticate_bearer_async,
)
from jp_adopt_api.auth_entra import TenantNotProvisionedError
from jp_adopt_api.config import Settings, get_settings
from jp_adopt_api.db import get_db
from jp_adopt_api.models import Role, UserRole

logger = logging.getLogger(__name__)

DbSession = Annotated[AsyncSession, Depends(get_db)]


def settings_dep() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(settings_dep)]


# Dev-local bearer carries no roles by default. We treat it as a super-user
# for local development so the staff UI is usable without seeding user_roles
# rows. This branch is gated on STRICT_AUTH=false / non-production via
# `authenticate_bearer`, so production never reaches it.
_DEV_LOCAL_SUB = "dev-local"
_DEV_LOCAL_ROLES = frozenset(
    {"staff_admin", "adoption_manager", "triage_facilitator", "facilitator"}
)


async def require_user(
    settings: SettingsDep,
    db: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> AuthUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Empty bearer token",
        )
    try:
        return await authenticate_bearer_async(db, token, settings)
    except DevelopmentAuthForbiddenError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Development bearer authentication is disabled in production",
        ) from None
    except TenantNotProvisionedError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "tenant_not_provisioned", "message": str(e)},
        ) from None
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
        ) from None
    except Exception:
        logger.exception("Unexpected error during bearer authentication")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service temporarily unavailable",
        ) from None


CurrentUser = Annotated[AuthUser, Depends(require_user)]


async def load_user_roles(db: AsyncSession, user_sub: str) -> frozenset[str]:
    """Resolve role names for the given B2C subject. Returns frozenset.

    Dev-local bearer is treated as a full-access actor so the staff UI is
    usable without seed data. Authentication already gates this branch on
    non-production / STRICT_AUTH=false.
    """
    if user_sub == _DEV_LOCAL_SUB:
        return _DEV_LOCAL_ROLES
    rows = await db.execute(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_b2c_subject_id == user_sub)
    )
    return frozenset(rows.scalars().all())


async def _resolve_current_user_roles(
    user: CurrentUser, db: DbSession
) -> tuple[AuthUser, frozenset[str]]:
    roles = await load_user_roles(db, user.sub)
    return user, roles


CurrentUserWithRoles = Annotated[
    tuple[AuthUser, frozenset[str]], Depends(_resolve_current_user_roles)
]


def require_role(
    *allowed: str,
) -> Callable[..., Coroutine[Any, Any, tuple[AuthUser, frozenset[str]]]]:
    """Dependency factory that enforces the actor has at least one of the
    listed role names. Returns the (user, roles) tuple so handlers can branch
    further on the full role set (e.g. staff_admin → see all, facilitator →
    see only their org).
    """
    allowed_set = frozenset(allowed)
    if not allowed_set:
        raise ValueError("require_role(...) needs at least one allowed role")

    async def _dep(
        user_with_roles: CurrentUserWithRoles,
    ) -> tuple[AuthUser, frozenset[str]]:
        user, roles = user_with_roles
        if not roles & allowed_set:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "role_required",
                    "message": f"Requires one of: {sorted(allowed_set)}",
                },
            )
        return user, roles

    return _dep
