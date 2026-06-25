"""Tests for the duplicate-email review admin endpoints.

A ``duplicate_email`` conflict is a DT-origin contact whose email collided
with an existing contact on import. These endpoints surface each as a pair and
record the reviewer's call in ``duplicate_review_decision``:

  * GET  /v1/admin/duplicate-conflicts          — list pairs (+ cluster size)
  * POST /v1/admin/duplicate-conflicts/decide    — merge | ignore
  * DELETE /v1/admin/duplicate-conflicts/decide  — undo
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jp_adopt_api.config import get_settings
from jp_adopt_api.main import app
from jp_adopt_api.models import (
    Contact,
    DuplicateReviewDecision,
    MigrationConflict,
)

os.environ.setdefault("STRICT_AUTH", "false")
os.environ.setdefault("APP_ENV", "development")
get_settings.cache_clear()

_OWNER_EMAIL = "dupreview-owner@x.dev"
_SHARED_EMAIL = "dupreview-shared@x.dev"
_DT_SINGLE = "dr-9501"
_DT_CLUSTER_A = "dr-9601"
_DT_CLUSTER_B = "dr-9602"


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer dev-local"}


def _dt_contact(source_id: str, name: str) -> Contact:
    return Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name=name,
        adopter_status="engaged",
        source_system="dt",
        source_id=source_id,
        email_normalized=None,
    )


@pytest_asyncio.fixture
async def seeded(session: AsyncSession) -> AsyncIterator[None]:
    """One single-collision conflict + a 2-record shared-email cluster."""
    owner = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name="Marta Forms",
        adopter_status="potential_adopter",
        email_normalized=_OWNER_EMAIL,
    )
    shared_owner = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name="Org Inbox",
        email_normalized=_SHARED_EMAIL,
    )
    contacts = [
        owner,
        shared_owner,
        _dt_contact(_DT_SINGLE, "Marta Dt"),
        _dt_contact(_DT_CLUSTER_A, "Cluster Person A"),
        _dt_contact(_DT_CLUSTER_B, "Cluster Person B"),
    ]
    conflicts = [
        MigrationConflict(
            id=uuid.uuid4(),
            source_system="dt",
            source_id=_DT_SINGLE,
            table_name="contacts",
            conflict_type="duplicate_email",
            source_value={"email_normalized": _OWNER_EMAIL},
        ),
        MigrationConflict(
            id=uuid.uuid4(),
            source_system="dt",
            source_id=_DT_CLUSTER_A,
            table_name="contacts",
            conflict_type="duplicate_email",
            source_value={"email_normalized": _SHARED_EMAIL},
        ),
        MigrationConflict(
            id=uuid.uuid4(),
            source_system="dt",
            source_id=_DT_CLUSTER_B,
            table_name="contacts",
            conflict_type="duplicate_email",
            source_value={"email_normalized": _SHARED_EMAIL},
        ),
    ]
    session.add_all([*contacts, *conflicts])
    await session.commit()
    try:
        yield
    finally:
        for email in (_OWNER_EMAIL, _SHARED_EMAIL):
            await session.execute(
                delete(DuplicateReviewDecision).where(
                    DuplicateReviewDecision.email_normalized == email
                )
            )
        await session.execute(
            delete(MigrationConflict).where(
                MigrationConflict.source_id.in_(
                    [_DT_SINGLE, _DT_CLUSTER_A, _DT_CLUSTER_B]
                )
            )
        )
        await session.execute(
            delete(Contact).where(
                Contact.email_normalized.in_([_OWNER_EMAIL, _SHARED_EMAIL])
            )
        )
        await session.execute(
            delete(Contact).where(
                Contact.source_id.in_(
                    [_DT_SINGLE, _DT_CLUSTER_A, _DT_CLUSTER_B]
                )
            )
        )
        await session.commit()


def _list(client: TestClient, **params: object) -> list[dict]:
    r = client.get(
        "/v1/admin/duplicate-conflicts",
        params={"limit": 500, **params},
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _find(items: list[dict], email: str, dt_source_id: str) -> dict | None:
    return next(
        (
            i
            for i in items
            if i["email"] == email and i["dt_source_id"] == dt_source_id
        ),
        None,
    )


@pytest.mark.asyncio
async def test_list_pairs_dt_and_owner(
    client: TestClient, seeded: None
) -> None:
    item = _find(_list(client), _OWNER_EMAIL, _DT_SINGLE)
    assert item is not None
    assert item["cluster_size"] == 1
    assert item["decision"] is None
    assert item["dt_contact"]["display_name"] == "Marta Dt"
    assert item["owner_contact"]["display_name"] == "Marta Forms"


@pytest.mark.asyncio
async def test_cluster_size_reflects_shared_inbox(
    client: TestClient, seeded: None
) -> None:
    item = _find(_list(client), _SHARED_EMAIL, _DT_CLUSTER_A)
    assert item is not None
    assert item["cluster_size"] == 2


@pytest.mark.asyncio
async def test_ignore_hides_then_include_ignored_shows(
    client: TestClient, seeded: None
) -> None:
    r = client.post(
        "/v1/admin/duplicate-conflicts/decide",
        json={
            "email": _OWNER_EMAIL,
            "dt_source_id": _DT_SINGLE,
            "decision": "ignore",
        },
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    # Default list excludes ignored.
    assert _find(_list(client), _OWNER_EMAIL, _DT_SINGLE) is None
    # include_ignored surfaces it with the decision attached.
    shown = _find(_list(client, include_ignored=True), _OWNER_EMAIL, _DT_SINGLE)
    assert shown is not None
    assert shown["decision"] == "ignore"


@pytest.mark.asyncio
async def test_merge_marks_then_undo_clears(
    client: TestClient, seeded: None
) -> None:
    client.post(
        "/v1/admin/duplicate-conflicts/decide",
        json={
            "email": _OWNER_EMAIL,
            "dt_source_id": _DT_SINGLE,
            "decision": "merge",
        },
        headers=_auth(),
    )
    queued = _find(_list(client), _OWNER_EMAIL, _DT_SINGLE)
    assert queued is not None and queued["decision"] == "merge"

    d = client.delete(
        "/v1/admin/duplicate-conflicts/decide",
        params={"email": _OWNER_EMAIL, "dt_source_id": _DT_SINGLE},
        headers=_auth(),
    )
    assert d.status_code == 204, d.text
    back = _find(_list(client), _OWNER_EMAIL, _DT_SINGLE)
    assert back is not None and back["decision"] is None


@pytest.mark.asyncio
async def test_merge_enforces_single_keeper_per_cluster(
    client: TestClient, seeded: None
) -> None:
    # Pick A as keeper, then B — only B should remain a merge decision.
    for src in (_DT_CLUSTER_A, _DT_CLUSTER_B):
        client.post(
            "/v1/admin/duplicate-conflicts/decide",
            json={
                "email": _SHARED_EMAIL,
                "dt_source_id": src,
                "decision": "merge",
            },
            headers=_auth(),
        )
    items = _list(client, include_ignored=True)
    a = _find(items, _SHARED_EMAIL, _DT_CLUSTER_A)
    b = _find(items, _SHARED_EMAIL, _DT_CLUSTER_B)
    assert b is not None and b["decision"] == "merge"
    assert a is not None and a["decision"] is None


@pytest.mark.asyncio
async def test_requires_staff_admin(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jp_adopt_api import deps as deps_module

    async def _fake_roles(db: object, user_sub: str) -> frozenset[str]:
        return frozenset({"facilitator"})

    monkeypatch.setattr(deps_module, "load_user_roles", _fake_roles)
    r = client.get("/v1/admin/duplicate-conflicts", headers=_auth())
    assert r.status_code == 403
