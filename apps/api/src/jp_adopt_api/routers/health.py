from __future__ import annotations

import os

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jp_adopt_api.db import get_db

router = APIRouter(tags=["health"])


# U12: surface the deploy SHA on /healthz so synthetic monitors can
# confirm which revision is live (and so an operator can curl the
# endpoint after a deploy to verify the new container actually came
# up). Set via the DEPLOY_SHA env var on the container — the GitHub
# Actions deploy.yml passes ``${{ github.sha }}`` into the ACA app
# settings on each deploy. Empty in dev.
_DEPLOY_SHA = os.environ.get("DEPLOY_SHA", "")
_DEPLOY_SHA_SHORT = _DEPLOY_SHA[:10] if _DEPLOY_SHA else ""


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe — does NOT check the database. The container's
    liveness signal should stay green during a transient Postgres
    outage so Azure doesn't restart the pod into a thundering-herd
    reconnect storm. Returns the deploy SHA when one is configured."""
    payload = {"status": "ok"}
    if _DEPLOY_SHA_SHORT:
        payload["sha"] = _DEPLOY_SHA_SHORT
    return payload


@router.get("/readyz")
async def readyz(db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    """Readiness probe — confirms Postgres is reachable. Synthetic
    monitors that page on database loss should target this endpoint
    (not /healthz). Azure Container Apps reads /readyz to decide whether
    to route traffic to the revision."""
    await db.execute(text("SELECT 1"))
    payload = {"status": "ready"}
    if _DEPLOY_SHA_SHORT:
        payload["sha"] = _DEPLOY_SHA_SHORT
    return payload
