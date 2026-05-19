"""Pytest setup: set DATABASE_URL before importing the FastAPI app."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Default matches docker-compose and CI Postgres (localhost from the test process).
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://jp_adopt:jp_adopt@127.0.0.1:5434/jp_adopt",
)

# Make the worker package importable in API test runs. The worker is a sibling
# uv workspace member that isn't installed into the API venv, but its source
# tree is right next door — exposing it on sys.path lets tests exercise worker
# tasks (e.g. the ARQ permanent_failure log path) without standing up a
# separate test runner. The API ``routers/auth_magic_link`` already imports
# the worker lazily at runtime via this same module path.
_WORKER_SRC = Path(__file__).resolve().parents[2] / "worker" / "src"
if _WORKER_SRC.is_dir() and str(_WORKER_SRC) not in sys.path:
    sys.path.insert(0, str(_WORKER_SRC))

import pytest  # noqa: E402  — must follow env / sys.path setup above.
from fastapi.testclient import TestClient  # noqa: E402

from jp_adopt_api.main import app  # noqa: E402


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
