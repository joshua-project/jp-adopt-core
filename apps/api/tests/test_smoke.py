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
