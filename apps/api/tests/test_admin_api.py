"""F15 — admin endpoints for facilitator-org onboarding.

Smoke coverage for:
  * ``GET /v1/facilitating-orgs`` — listing active orgs.
  * ``POST /v1/admin/facilitator-memberships`` — granting membership.
  * ``DELETE /v1/admin/facilitator-memberships/{user}/{org}`` — revocation.
  * 403 when the caller lacks staff_admin.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jp_adopt_api.config import get_settings
from jp_adopt_api.main import app
from jp_adopt_api.models import FacilitatingOrg, FacilitatorOrgMembership

os.environ.setdefault("STRICT_AUTH", "false")
os.environ.setdefault("APP_ENV", "development")
get_settings.cache_clear()


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def _auth_headers(token: str = "dev-local") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def _seeded_org(session: AsyncSession) -> AsyncIterator[FacilitatingOrg]:
    org = FacilitatingOrg(
        id=uuid.uuid4(),
        name=f"TST Admin Org {uuid.uuid4().hex[:6]}",
        capacity_total=5,
        active=True,
    )
    session.add(org)
    await session.commit()
    try:
        yield org
    finally:
        await session.execute(
            delete(FacilitatorOrgMembership).where(
                FacilitatorOrgMembership.facilitator_org_id == org.id
            )
        )
        await session.execute(
            delete(FacilitatingOrg).where(FacilitatingOrg.id == org.id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_list_facilitating_orgs_returns_active_orgs(
    client: TestClient, _seeded_org: FacilitatingOrg
) -> None:
    r = client.get("/v1/facilitating-orgs", headers=_auth_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {item["id"] for item in body["items"]}
    assert str(_seeded_org.id) in ids


@pytest.mark.asyncio
async def test_list_facilitating_orgs_requires_staff_admin(
    client: TestClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jp_adopt_api import deps as deps_module

    async def _fake_roles(db: object, user_sub: str) -> frozenset[str]:
        # Non-admin caller — must be rejected by require_role("staff_admin").
        return frozenset({"facilitator"})

    monkeypatch.setattr(deps_module, "load_user_roles", _fake_roles)
    r = client.get("/v1/facilitating-orgs", headers=_auth_headers())
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_create_facilitator_membership_grants_access(
    client: TestClient,
    session: AsyncSession,
    _seeded_org: FacilitatingOrg,
) -> None:
    user_sub = f"user-{uuid.uuid4().hex[:8]}"
    r = client.post(
        "/v1/admin/facilitator-memberships",
        json={
            "user_subject_id": user_sub,
            "facilitator_org_id": str(_seeded_org.id),
        },
        headers=_auth_headers(),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user_subject_id"] == user_sub
    assert body["facilitator_org_id"] == str(_seeded_org.id)
    assert body["role_in_org"] == "member"
    # Persisted.
    existing = (
        await session.execute(
            select(FacilitatorOrgMembership).where(
                FacilitatorOrgMembership.user_subject_id == user_sub
            )
        )
    ).scalar_one_or_none()
    assert existing is not None


@pytest.mark.asyncio
async def test_create_facilitator_membership_duplicate_returns_409(
    client: TestClient,
    session: AsyncSession,
    _seeded_org: FacilitatingOrg,
) -> None:
    user_sub = f"user-{uuid.uuid4().hex[:8]}"
    payload = {
        "user_subject_id": user_sub,
        "facilitator_org_id": str(_seeded_org.id),
    }
    r1 = client.post(
        "/v1/admin/facilitator-memberships",
        json=payload,
        headers=_auth_headers(),
    )
    assert r1.status_code == 201, r1.text
    r2 = client.post(
        "/v1/admin/facilitator-memberships",
        json=payload,
        headers=_auth_headers(),
    )
    assert r2.status_code == 409, r2.text
    assert r2.json()["detail"]["code"] == "membership_already_exists"


@pytest.mark.asyncio
async def test_delete_facilitator_membership_revokes(
    client: TestClient,
    session: AsyncSession,
    _seeded_org: FacilitatingOrg,
) -> None:
    user_sub = f"user-{uuid.uuid4().hex[:8]}"
    session.add(
        FacilitatorOrgMembership(
            user_subject_id=user_sub,
            facilitator_org_id=_seeded_org.id,
        )
    )
    await session.commit()
    r = client.delete(
        f"/v1/admin/facilitator-memberships/{user_sub}/{_seeded_org.id}",
        headers=_auth_headers(),
    )
    assert r.status_code == 204, r.text
    remaining = (
        await session.execute(
            select(FacilitatorOrgMembership).where(
                FacilitatorOrgMembership.user_subject_id == user_sub
            )
        )
    ).scalar_one_or_none()
    assert remaining is None


@pytest.mark.asyncio
async def test_delete_facilitator_membership_idempotent(
    client: TestClient,
    _seeded_org: FacilitatingOrg,
) -> None:
    missing_user = f"user-{uuid.uuid4().hex[:8]}"
    r = client.delete(
        f"/v1/admin/facilitator-memberships/{missing_user}/{_seeded_org.id}",
        headers=_auth_headers(),
    )
    assert r.status_code == 204
