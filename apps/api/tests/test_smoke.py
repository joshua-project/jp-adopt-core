"""CI smoke tests: public health, DB readiness, and auth on protected routes."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_v1_contacts_requires_auth(client: TestClient) -> None:
    response = client.get("/v1/contacts")
    assert response.status_code == 401


SEED_CONTACT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def test_patch_rejects_null_for_non_nullable_columns(client: TestClient) -> None:
    """Copilot: PATCH with null display_name must be 422, not 500 IntegrityError."""
    response = client.patch(
        f"/v1/contacts/{SEED_CONTACT_ID}",
        headers={"Authorization": "Bearer dev-local"},
        json={"display_name": None},
    )
    assert response.status_code == 422
