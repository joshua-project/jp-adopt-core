"""Sync Postgres reader for jp-adopt-forms source database.

The forms repo stores submissions in two normalized tables (not a single
``submissions`` JSONB table):

* ``adoption_submissions`` + ``adoption_fpg_selections``
* ``facilitation_submissions`` + ``facilitation_fpg_selections``

This module yields unified rows (``form_type`` + submission columns +
aggregated ``fpg_selections`` JSON) ordered by ``created_at ASC`` so
watermark-based incremental runs stay stable.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 100

SOURCE_SYSTEM = "jp-adopt-forms"

_ADOPTION_SQL = """
SELECT
    'adoption'::text AS form_type,
    s.id::text AS id,
    s.submission_id,
    s.created_at,
    s.updated_at,
    row_to_json(s)::jsonb AS submission,
    COALESCE(
        json_agg(
            json_build_object(
                'people_id3', f.people_id3,
                'people_group_name', f.people_group_name,
                'country', f.country,
                'commitment_types', f.commitment_types
            )
            ORDER BY f.created_at
        ) FILTER (WHERE f.id IS NOT NULL),
        '[]'::json
    ) AS fpg_selections
FROM adoption_submissions s
LEFT JOIN adoption_fpg_selections f ON f.submission_id = s.id
WHERE (:watermark IS NULL OR s.created_at > :watermark)
GROUP BY s.id
ORDER BY s.created_at ASC, s.id ASC
"""

_FACILITATION_SQL = """
SELECT
    'facilitation'::text AS form_type,
    s.id::text AS id,
    s.submission_id,
    s.created_at,
    s.updated_at,
    row_to_json(s)::jsonb AS submission,
    COALESCE(
        json_agg(
            json_build_object(
                'people_id3', f.people_id3,
                'people_group_name', f.people_group_name,
                'country', f.country,
                'engagement_status', f.engagement_status,
                'can_facilitate', f.can_facilitate,
                'facilitation_services', f.facilitation_services,
                'network_services', f.network_services
            )
            ORDER BY f.created_at
        ) FILTER (WHERE f.id IS NOT NULL),
        '[]'::json
    ) AS fpg_selections
FROM facilitation_submissions s
LEFT JOIN facilitation_fpg_selections f ON f.submission_id = s.id
WHERE (:watermark IS NULL OR s.created_at > :watermark)
GROUP BY s.id
ORDER BY s.created_at ASC, s.id ASC
"""


def open_engine(postgres_url: str) -> Engine:
    """Open a sync SQLAlchemy engine against the forms Postgres source."""
    return create_engine(postgres_url, pool_pre_ping=True, future=True)


def _iter_query_rows(
    conn: Connection,
    sql: str,
    *,
    watermark: datetime | None,
    batch_size: int,
) -> Iterator[dict[str, Any]]:
    """Stream rows from the source query. ``batch_size`` becomes the cursor
    fetch-buffer hint (via ``max_row_buffer``); the loop itself yields one
    row at a time, no Python-side accumulation."""
    params: dict[str, Any] = {"watermark": watermark}
    result = conn.execution_options(
        stream_results=True, max_row_buffer=batch_size
    ).execute(text(sql), params)
    for row in result.mappings():
        yield dict(row)


def _merge_by_created_at(
    adoption_rows: list[dict[str, Any]],
    facilitation_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge pre-materialized rows (used in unit tests)."""
    merged: list[dict[str, Any]] = []
    i = j = 0
    while i < len(adoption_rows) or j < len(facilitation_rows):
        if j >= len(facilitation_rows) or (
            i < len(adoption_rows)
            and adoption_rows[i]["created_at"] <= facilitation_rows[j]["created_at"]
        ):
            merged.append(adoption_rows[i])
            i += 1
        else:
            merged.append(facilitation_rows[j])
            j += 1
    return merged


def _merge_streams(
    adoption_iter: Iterator[dict[str, Any]],
    facilitation_iter: Iterator[dict[str, Any]],
) -> Iterator[dict[str, Any]]:
    """Merge two sorted submission streams by ``created_at`` without buffering."""
    adoption_next = next(adoption_iter, None)
    facilitation_next = next(facilitation_iter, None)
    while adoption_next is not None or facilitation_next is not None:
        if facilitation_next is None or (
            adoption_next is not None
            and adoption_next["created_at"] <= facilitation_next["created_at"]
        ):
            yield adoption_next
            adoption_next = next(adoption_iter, None)
        else:
            yield facilitation_next
            facilitation_next = next(facilitation_iter, None)


def iter_submissions(
    conn: Connection,
    *,
    watermark: datetime | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Iterator[dict[str, Any]]:
    """Yield forms submission rows newest-last, stable on ``created_at``.

    Each row includes ``form_type`` (``adoption`` | ``facilitation``),
    ``id`` (forms DB UUID as text), ``submission_id`` (public idempotency
    key), ``created_at``, ``updated_at``, ``submission`` (full row JSON),
    and ``fpg_selections`` (list of selection dicts).
    """
    adoption_iter = _iter_query_rows(
        conn, _ADOPTION_SQL, watermark=watermark, batch_size=batch_size
    )
    facilitation_iter = _iter_query_rows(
        conn, _FACILITATION_SQL, watermark=watermark, batch_size=batch_size
    )
    yield from _merge_streams(adoption_iter, facilitation_iter)


def fetch_max_created_at(conn: Connection) -> datetime | None:
    """Highest ``created_at`` across both submission tables (next watermark)."""
    sql = text(
        """
        SELECT MAX(ts) AS max_ts FROM (
            SELECT MAX(created_at) AS ts FROM adoption_submissions
            UNION ALL
            SELECT MAX(created_at) AS ts FROM facilitation_submissions
        ) AS combined
        """
    )
    row = conn.execute(sql).mappings().one_or_none()
    return row["max_ts"] if row and row.get("max_ts") else None


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "SOURCE_SYSTEM",
    "fetch_max_created_at",
    "iter_submissions",
    "open_engine",
]
