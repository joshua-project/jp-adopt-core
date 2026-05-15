"""Pytest setup: set DATABASE_URL before importing the FastAPI app."""

from __future__ import annotations

import os

# Default matches docker-compose and CI Postgres (localhost from the test process).
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://jp_adopt:jp_adopt@127.0.0.1:5434/jp_adopt",
)

import pytest
from fastapi.testclient import TestClient

from jp_adopt_api.main import app


@pytest.fixture(autouse=True)
def _reset_app_db_engine():
    """The app-level engine in ``jp_adopt_api.db`` is a module-global cache.
    pytest tests that combine TestClient(app) with async fixtures often each
    spin up a fresh asyncio loop; the cached engine binds asyncpg connections
    to whichever loop created them. Reset the cache before every test so each
    test's TestClient (or async fixture) gets a fresh engine in its own loop.
    """
    import jp_adopt_api.db as appdb

    appdb._engine = None
    appdb._session_factory = None
    yield
    appdb._engine = None
    appdb._session_factory = None


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c
