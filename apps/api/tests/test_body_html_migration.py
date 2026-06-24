"""Tests for 0032 (campaign_step.body_html column + seed-from-templates).

Assumes `alembic upgrade head` ran before the session (conftest runs against a
migrated dev DB). Schema-shape checks use raw SQL; the seed logic is exercised
directly through the migration's `_extract_body` helper plus a round-trip
against a real template file.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from jp_adopt_api.config import get_settings

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20260624_0032_campaign_step_body_html.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig_0032", _MIGRATION_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def conn() -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as connection:
        yield connection
        await connection.rollback()
    await engine.dispose()


async def test_campaign_step_has_body_html_and_nullable_template(
    conn: AsyncConnection,
) -> None:
    result = await conn.execute(
        text(
            """
            SELECT column_name, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'campaign_step'
              AND column_name IN ('body_html', 'mjml_template_name')
            """
        )
    )
    cols = {row[0]: row[1] for row in result.all()}
    assert cols.get("body_html") == "YES"
    # mjml_template_name relaxed to nullable so body-only steps are valid.
    assert cols.get("mjml_template_name") == "YES"


def test_extract_body_pulls_block_body_from_real_template() -> None:
    mig = _load_migration()
    body = mig._extract_body("facilitator-welcome.step-0.mjml")
    assert body is not None
    # The seeded body is the inner block content, with the token preserved and
    # the {% block %}/{% extends %} chrome stripped.
    assert "{{ contact_display_name }}" in body
    assert "Welcome to Joshua Project Adoption" in body
    assert "{% block" not in body
    assert "{% extends" not in body


def test_extract_body_returns_none_for_missing_template() -> None:
    mig = _load_migration()
    assert mig._extract_body("does-not-exist.step-99.mjml") is None


async def test_seed_populates_then_is_idempotent(conn: AsyncConnection) -> None:
    """A NULL-body step referencing a real template gets seeded; re-running the
    seed UPDATE (guarded by body_html IS NULL) is a no-op."""
    mig = _load_migration()
    campaign_id = uuid.uuid4()
    step_id = uuid.uuid4()
    template = "facilitator-welcome.step-0.mjml"
    try:
        await conn.execute(
            text(
                "INSERT INTO campaign (id, name, status) "
                "VALUES (:id, :name, 'draft')"
            ),
            {"id": campaign_id, "name": "seed-test"},
        )
        await conn.execute(
            text(
                "INSERT INTO campaign_step "
                "(id, campaign_id, position, mjml_template_name, subject, body_html) "
                "VALUES (:id, :cid, 0, :tpl, 'subj', NULL)"
            ),
            {"id": step_id, "cid": campaign_id, "tpl": template},
        )

        body = mig._extract_body(template)
        assert body is not None
        seed_sql = text(
            "UPDATE campaign_step SET body_html = :body "
            "WHERE id = :id AND body_html IS NULL"
        )
        res1 = await conn.execute(seed_sql, {"body": body, "id": step_id})
        assert res1.rowcount == 1

        stored = (
            await conn.execute(
                text("SELECT body_html FROM campaign_step WHERE id = :id"),
                {"id": step_id},
            )
        ).scalar_one()
        assert "{{ contact_display_name }}" in stored

        # Idempotent: the NULL guard means a second run touches nothing.
        res2 = await conn.execute(
            seed_sql, {"body": "OVERWRITE", "id": step_id}
        )
        assert res2.rowcount == 0
    finally:
        await conn.execute(
            text("DELETE FROM campaign_step WHERE id = :id"), {"id": step_id}
        )
        await conn.execute(
            text("DELETE FROM campaign WHERE id = :id"), {"id": campaign_id}
        )
        await conn.commit()
