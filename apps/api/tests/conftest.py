"""Pytest setup: set DATABASE_URL before importing the FastAPI app."""

from __future__ import annotations

import os

# Default matches docker-compose and CI Postgres (localhost from the test process).
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://jp_adopt:jp_adopt@127.0.0.1:5432/jp_adopt",
)

import pytest
from fastapi.testclient import TestClient

from jp_adopt_api.main import app


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c
