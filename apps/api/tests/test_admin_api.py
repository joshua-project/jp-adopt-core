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
from jp_adopt_api.models import (
    FacilitatingOrg,
    FacilitatorOrgMembership,
    Outbox,
    Role,
    UserRole,
)

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


STAFF_ADMIN_ROLE_ID = uuid.UUID("00000003-0000-0000-0000-000000000001")
FACILITATOR_ROLE_ID = uuid.UUID("00000003-0000-0000-0000-000000000004")


@pytest.mark.asyncio
async def test_list_user_roles_returns_grants_ordered(
    client: TestClient, session: AsyncSession
) -> None:
    user_a = f"user-{uuid.uuid4().hex[:8]}"
    user_b = f"user-{uuid.uuid4().hex[:8]}"
    fac_id = (
        await session.execute(select(Role).where(Role.name == "facilitator"))
    ).scalar_one().id
    session.add_all(
        [
            UserRole(user_subject_id=user_a, role_id=STAFF_ADMIN_ROLE_ID),
            UserRole(user_subject_id=user_b, role_id=fac_id),
        ]
    )
    await session.commit()
    try:
        r = client.get("/v1/admin/user-roles", headers=_auth_headers())
        assert r.status_code == 200, r.text
        body = r.json()
        subs = {item["user_subject_id"] for item in body["items"]}
        assert user_a in subs
        assert user_b in subs
        names = {
            item["role_name"]
            for item in body["items"]
            if item["user_subject_id"] in {user_a, user_b}
        }
        assert "staff_admin" in names
        assert "facilitator" in names
    finally:
        await session.execute(
            delete(UserRole).where(
                UserRole.user_subject_id.in_([user_a, user_b])
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_list_roles_returns_seeded_roles(
    client: TestClient,
) -> None:
    r = client.get("/v1/admin/roles", headers=_auth_headers())
    assert r.status_code == 200, r.text
    names = {item["name"] for item in r.json()["items"]}
    assert "staff_admin" in names
    assert "facilitator" in names


@pytest.mark.asyncio
async def test_list_roles_requires_staff_admin(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jp_adopt_api import deps as deps_module

    async def _fake_roles(db: object, user_sub: str) -> frozenset[str]:
        return frozenset({"facilitator"})

    monkeypatch.setattr(deps_module, "load_user_roles", _fake_roles)
    r = client.get("/v1/admin/roles", headers=_auth_headers())
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "role_required"


@pytest.mark.asyncio
async def test_list_user_roles_requires_staff_admin(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jp_adopt_api import deps as deps_module

    async def _fake_roles(db: object, user_sub: str) -> frozenset[str]:
        return frozenset({"facilitator"})

    monkeypatch.setattr(deps_module, "load_user_roles", _fake_roles)
    r = client.get("/v1/admin/user-roles", headers=_auth_headers())
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "role_required"


# ─── Graph enrichment (#97) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_user_roles_unconfigured_graph_returns_oid_only(
    client: TestClient, session: AsyncSession
) -> None:
    """When AZURE_GRAPH_* env vars are unset (the default in CI),
    list_user_roles still works — display name and UPN are null and
    graph_enriched is False. No Graph network call is attempted."""
    user_id = f"oid-{uuid.uuid4()}"
    session.add(UserRole(user_subject_id=user_id, role_id=STAFF_ADMIN_ROLE_ID))
    await session.commit()
    try:
        r = client.get("/v1/admin/user-roles", headers=_auth_headers())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["graph_enriched"] is False
        row = next(item for item in body["items"] if item["user_subject_id"] == user_id)
        assert row["user_display_name"] is None
        assert row["user_principal_name"] is None
    finally:
        await session.execute(delete(UserRole).where(UserRole.user_subject_id == user_id))
        await session.commit()


@pytest.mark.asyncio
async def test_list_user_roles_enriches_when_graph_returns_users(
    client: TestClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Graph is reachable, each row carries display_name + UPN
    and graph_enriched is True."""
    from jp_adopt_api.routers import admin as admin_router
    from jp_adopt_api.graph import GraphUser

    user_id = f"oid-{uuid.uuid4()}"
    session.add(UserRole(user_subject_id=user_id, role_id=STAFF_ADMIN_ROLE_ID))
    await session.commit()

    async def _stub_lookup(ids: list[str]) -> dict[str, GraphUser]:
        # Echo back a fabricated user for each requested OID.
        return {
            oid: GraphUser(
                id=oid,
                display_name=f"Display {oid[-8:]}",
                user_principal_name=f"user-{oid[-8:]}@example.com",
                mail=f"user-{oid[-8:]}@example.com",
            )
            for oid in ids
        }

    monkeypatch.setattr(admin_router, "lookup_users_by_ids", _stub_lookup)
    try:
        r = client.get("/v1/admin/user-roles", headers=_auth_headers())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["graph_enriched"] is True
        row = next(item for item in body["items"] if item["user_subject_id"] == user_id)
        assert row["user_display_name"] == f"Display {user_id[-8:]}"
        assert row["user_principal_name"] == f"user-{user_id[-8:]}@example.com"
    finally:
        await session.execute(delete(UserRole).where(UserRole.user_subject_id == user_id))
        await session.commit()


@pytest.mark.asyncio
async def test_search_directory_users_returns_empty_when_unconfigured(
    client: TestClient,
) -> None:
    r = client.get("/v1/admin/users/search?q=amy", headers=_auth_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"] == []
    assert body["graph_configured"] is False


@pytest.mark.asyncio
async def test_search_directory_users_returns_graph_results(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jp_adopt_api.routers import admin as admin_router
    from jp_adopt_api.graph import GraphUser

    async def _stub_search(q: str, *, limit: int = 10) -> list[GraphUser]:
        if q != "amy":
            return []
        return [
            GraphUser(
                id="oid-amy",
                display_name="Amy Adopter",
                user_principal_name="amy@globalspecifics.com",
                mail="amy@globalspecifics.com",
            )
        ]

    monkeypatch.setattr(admin_router, "search_users", _stub_search)
    monkeypatch.setattr(admin_router, "graph_configured", lambda: True)
    r = client.get("/v1/admin/users/search?q=amy", headers=_auth_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["graph_configured"] is True
    assert len(body["items"]) == 1
    assert body["items"][0]["user_subject_id"] == "oid-amy"
    assert body["items"][0]["display_name"] == "Amy Adopter"


@pytest.mark.asyncio
async def test_search_directory_users_requires_staff_admin(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jp_adopt_api import deps as deps_module

    async def _fake_roles(db: object, user_sub: str) -> frozenset[str]:
        return frozenset({"facilitator"})

    monkeypatch.setattr(deps_module, "load_user_roles", _fake_roles)
    r = client.get("/v1/admin/users/search?q=amy", headers=_auth_headers())
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_grant_user_role_happy_path(
    client: TestClient, session: AsyncSession
) -> None:
    user_sub = str(uuid.uuid4())
    r = client.post(
        "/v1/admin/user-roles",
        json={
            "user_subject_id": user_sub,
            "role_id": str(FACILITATOR_ROLE_ID),
        },
        headers=_auth_headers(),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user_subject_id"] == user_sub
    assert body["role_name"] == "facilitator"
    list_r = client.get("/v1/admin/user-roles", headers=_auth_headers())
    assert any(
        i["user_subject_id"] == user_sub for i in list_r.json()["items"]
    )
    outbox = (
        await session.execute(
            select(Outbox).where(Outbox.event_type == "admin.role.granted")
        )
    ).scalars().all()
    assert any(
        o.payload_json.get("target_subject_id") == user_sub for o in outbox
    )
    await session.execute(
        delete(UserRole).where(UserRole.user_subject_id == user_sub)
    )
    await session.execute(
        delete(Outbox).where(Outbox.event_type == "admin.role.granted")
    )
    await session.commit()


@pytest.mark.asyncio
async def test_grant_user_role_idempotent_emits_single_outbox_row(
    client: TestClient, session: AsyncSession
) -> None:
    """Re-granting an existing (subject, role) pair returns 201 with the
    existing row but does NOT emit a second outbox event — the audit log
    represents real state changes, not request attempts."""
    user_sub = str(uuid.uuid4())
    payload = {
        "user_subject_id": user_sub,
        "role_id": str(FACILITATOR_ROLE_ID),
    }
    r1 = client.post(
        "/v1/admin/user-roles", json=payload, headers=_auth_headers()
    )
    r2 = client.post(
        "/v1/admin/user-roles", json=payload, headers=_auth_headers()
    )
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text
    rows = (
        await session.execute(
            select(UserRole).where(UserRole.user_subject_id == user_sub)
        )
    ).scalars().all()
    assert len(rows) == 1
    granted_events = [
        o
        for o in (
            await session.execute(
                select(Outbox).where(Outbox.event_type == "admin.role.granted")
            )
        ).scalars().all()
        if o.payload_json.get("target_subject_id") == user_sub
    ]
    assert len(granted_events) == 1
    await session.execute(
        delete(UserRole).where(UserRole.user_subject_id == user_sub)
    )
    await session.execute(
        delete(Outbox).where(Outbox.event_type == "admin.role.granted")
    )
    await session.commit()


@pytest.mark.asyncio
async def test_grant_user_role_requires_staff_admin(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jp_adopt_api import deps as deps_module

    async def _fake_roles(db: object, user_sub: str) -> frozenset[str]:
        return frozenset({"facilitator"})

    monkeypatch.setattr(deps_module, "load_user_roles", _fake_roles)
    r = client.post(
        "/v1/admin/user-roles",
        json={
            "user_subject_id": str(uuid.uuid4()),
            "role_id": str(FACILITATOR_ROLE_ID),
        },
        headers=_auth_headers(),
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "role_required"


@pytest.mark.asyncio
async def test_grant_user_role_unknown_role_returns_404(
    client: TestClient,
) -> None:
    r = client.post(
        "/v1/admin/user-roles",
        json={
            "user_subject_id": str(uuid.uuid4()),
            "role_id": str(uuid.uuid4()),
        },
        headers=_auth_headers(),
    )
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["code"] == "role_not_found"


@pytest.mark.asyncio
async def test_grant_user_role_empty_subject_returns_422(
    client: TestClient,
) -> None:
    r = client.post(
        "/v1/admin/user-roles",
        json={"user_subject_id": "", "role_id": str(FACILITATOR_ROLE_ID)},
        headers=_auth_headers(),
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_grant_user_role_invalid_oid_returns_422(
    client: TestClient,
) -> None:
    r = client.post(
        "/v1/admin/user-roles",
        json={
            "user_subject_id": "not-a-uuid",
            "role_id": str(FACILITATOR_ROLE_ID),
        },
        headers=_auth_headers(),
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_revoke_own_staff_admin_missing_grant_returns_404(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jp_adopt_api import deps as deps_module
    from jp_adopt_api.auth import AuthUser

    admin_sub = str(uuid.uuid4())

    async def _fake_auth(
        db: object, token: str, settings: object
    ) -> AuthUser:
        if token == "dev-local":
            return AuthUser(sub=admin_sub)
        raise AssertionError("unexpected token in test")

    async def _fake_roles(db: object, user_sub: str) -> frozenset[str]:
        return frozenset({"staff_admin"})

    monkeypatch.setattr(deps_module, "authenticate_bearer_async", _fake_auth)
    monkeypatch.setattr(deps_module, "load_user_roles", _fake_roles)

    r = client.delete(
        f"/v1/admin/user-roles/{admin_sub}/{STAFF_ADMIN_ROLE_ID}",
        headers=_auth_headers(),
    )
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["code"] == "user_role_not_found"


@pytest.mark.asyncio
async def test_revoke_user_role_happy_path(
    client: TestClient, session: AsyncSession
) -> None:
    user_sub = str(uuid.uuid4())
    session.add(
        UserRole(user_subject_id=user_sub, role_id=FACILITATOR_ROLE_ID)
    )
    await session.commit()
    r = client.delete(
        f"/v1/admin/user-roles/{user_sub}/{FACILITATOR_ROLE_ID}",
        headers=_auth_headers(),
    )
    assert r.status_code == 204, r.text
    remaining = (
        await session.execute(
            select(UserRole).where(UserRole.user_subject_id == user_sub)
        )
    ).scalar_one_or_none()
    assert remaining is None
    revoked = (
        await session.execute(
            select(Outbox).where(Outbox.event_type == "admin.role.revoked")
        )
    ).scalars().all()
    assert any(
        o.payload_json.get("target_subject_id") == user_sub for o in revoked
    )


@pytest.mark.asyncio
async def test_revoke_user_role_requires_staff_admin(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jp_adopt_api import deps as deps_module

    async def _fake_roles(db: object, user_sub: str) -> frozenset[str]:
        return frozenset({"facilitator"})

    monkeypatch.setattr(deps_module, "load_user_roles", _fake_roles)
    r = client.delete(
        f"/v1/admin/user-roles/missing-user/{FACILITATOR_ROLE_ID}",
        headers=_auth_headers(),
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "role_required"


@pytest.mark.asyncio
async def test_revoke_user_role_not_found_returns_404(
    client: TestClient,
) -> None:
    r = client.delete(
        f"/v1/admin/user-roles/missing-user/{FACILITATOR_ROLE_ID}",
        headers=_auth_headers(),
    )
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["code"] == "user_role_not_found"


@pytest.mark.asyncio
async def test_revoke_user_role_unknown_role_returns_404(
    client: TestClient,
) -> None:
    r = client.delete(
        f"/v1/admin/user-roles/missing-user/{uuid.uuid4()}",
        headers=_auth_headers(),
    )
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["code"] == "role_not_found"


@pytest.mark.asyncio
async def test_revoke_own_staff_admin_forbidden(
    client: TestClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jp_adopt_api import deps as deps_module
    from jp_adopt_api.auth import AuthUser

    admin_sub = str(uuid.uuid4())
    session.add(
        UserRole(user_subject_id=admin_sub, role_id=STAFF_ADMIN_ROLE_ID)
    )
    await session.commit()

    async def _fake_auth(
        db: object, token: str, settings: object
    ) -> AuthUser:
        if token == "dev-local":
            return AuthUser(sub=admin_sub)
        raise AssertionError("unexpected token in test")

    monkeypatch.setattr(deps_module, "authenticate_bearer_async", _fake_auth)

    r = client.delete(
        f"/v1/admin/user-roles/{admin_sub}/{STAFF_ADMIN_ROLE_ID}",
        headers=_auth_headers(),
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "self_revoke_forbidden"
    remaining = (
        await session.execute(
            select(UserRole).where(UserRole.user_subject_id == admin_sub)
        )
    ).scalar_one_or_none()
    assert remaining is not None
    await session.execute(
        delete(UserRole).where(UserRole.user_subject_id == admin_sub)
    )
    await session.commit()
