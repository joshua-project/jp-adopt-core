from __future__ import annotations

import logging
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from jp_adopt_api.auth import (
    AuthUser,
    DevelopmentAuthForbiddenError,
    authenticate_bearer_async,
)
from jp_adopt_api.auth_entra import TenantNotProvisionedError
from jp_adopt_api.config import Settings, get_settings
from jp_adopt_api.db import get_db

logger = logging.getLogger(__name__)

DbSession = Annotated[AsyncSession, Depends(get_db)]


def settings_dep() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(settings_dep)]


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
