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


def test_patch_rejects_removed_status_fields(client: TestClient) -> None:
    """N2: ``adopter_status`` and ``facilitator_status`` were intentionally
    dropped from ``ContactPatch`` so a generic PATCH cannot bypass the state-
    machine. Without ``extra='forbid'`` Pydantic silently drops the unknown
    key and returns 200 — hiding the bypass attempt. Confirm the patch surface
    rejects the removed keys with 422 and names the offending field."""
    response = client.patch(
        f"/v1/contacts/{SEED_CONTACT_ID}",
        headers={"Authorization": "Bearer dev-local"},
        json={"adopter_status": "matched"},
    )
    assert response.status_code == 422
    # FastAPI 422 envelope (list of errors with `loc`) names the offending
    # field somewhere in the response body.
    assert "adopter_status" in response.text


def test_patch_rejects_facilitator_status_field(client: TestClient) -> None:
    """N2: companion to the above for ``facilitator_status``."""
    response = client.patch(
        f"/v1/contacts/{SEED_CONTACT_ID}",
        headers={"Authorization": "Bearer dev-local"},
        json={"facilitator_status": "ready"},
    )
    assert response.status_code == 422
    assert "facilitator_status" in response.text
