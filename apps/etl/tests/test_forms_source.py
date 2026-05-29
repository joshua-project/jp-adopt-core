"""Unit tests for forms_source row iteration."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from jp_adopt_etl.forms_source import _merge_by_created_at, iter_submissions


def test_merge_by_created_at_interleaves() -> None:
    t1 = datetime(2024, 1, 1, tzinfo=UTC)
    t2 = datetime(2024, 1, 2, tzinfo=UTC)
    adoption = [{"form_type": "adoption", "created_at": t2, "id": "a"}]
    facilitation = [{"form_type": "facilitation", "created_at": t1, "id": "f"}]
    merged = _merge_by_created_at(adoption, facilitation)
    assert [r["id"] for r in merged] == ["f", "a"]


def test_iter_submissions_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = MagicMock()

    def _empty(*args, **kwargs):
        return iter([])

    monkeypatch.setattr(
        "jp_adopt_etl.forms_source._iter_query_rows", lambda *a, **k: _empty()
    )
    rows = list(iter_submissions(conn))
    assert rows == []


def test_iter_submissions_paginated_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = MagicMock()
    base = datetime(2024, 6, 1, tzinfo=UTC)
    adoption_rows = [
        {"form_type": "adoption", "created_at": base, "id": f"a{i}"}
        for i in range(150)
    ]
    facilitation_rows = [
        {"form_type": "facilitation", "created_at": base, "id": f"f{i}"}
        for i in range(100)
    ]

    def _adoption(*args, **kwargs):
        return iter(adoption_rows)

    def _facilitation(*args, **kwargs):
        return iter(facilitation_rows)

    calls = {"n": 0}

    def _iter_side_effect(conn, sql, **kwargs):
        calls["n"] += 1
        if "adoption_submissions" in sql:
            return _adoption()
        return _facilitation()

    monkeypatch.setattr(
        "jp_adopt_etl.forms_source._iter_query_rows", _iter_side_effect
    )
    rows = list(iter_submissions(conn, batch_size=100))
    assert len(rows) == 250
    assert calls["n"] == 2


def test_open_engine_invalid_dsn() -> None:
    from jp_adopt_etl.forms_source import open_engine

    engine = open_engine("postgresql+psycopg2://invalid:invalid@127.0.0.1:1/nope")
    with pytest.raises(OperationalError):
        with engine.connect() as conn:
            conn.execute(__import__("sqlalchemy").text("SELECT 1"))
