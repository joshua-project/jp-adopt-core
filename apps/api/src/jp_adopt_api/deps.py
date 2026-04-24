from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from jp_adopt_api.auth import AuthUser, authenticate_bearer
from jp_adopt_api.config import Settings, get_settings
from jp_adopt_api.db import get_db
from sqlalchemy.ext.asyncio import AsyncSession

DbSession = Annotated[AsyncSession, Depends(get_db)]


def settings_dep() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(settings_dep)]


async def require_user(
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
) -> AuthUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Empty bearer token")
    try:
        return authenticate_bearer(token, settings)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
        ) from None


CurrentUser = Annotated[AuthUser, Depends(require_user)]
