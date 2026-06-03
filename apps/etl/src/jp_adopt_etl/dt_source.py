"""Sync MySQL reader for the DT source database.

Two design choices worth calling out:

1. **Raw SQL via SQLAlchemy ``text()``** instead of declarative ORM.
   DT's wp_* schema is not something we control; reflecting it produces
   the right Tables but burns startup time and obscures the actual
   columns we read. The plan calls for raw SQL with a WHERE filter on
   ``post_type``; that's clearer to read and audit during cutover.

2. **One open transaction per "batch"** rather than streaming a single
   cursor. Batches of ~500 rows let the orchestrator chunk
   wp_postmeta lookups (which are the expensive part) without holding a
   long-running transaction on the source DB. DT installations are
   read-only during the cutover window so transaction isolation isn't a
   concern; the batching is purely a memory bound.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

# Reasonable default; orchestrator can override via --batch-size flag.
DEFAULT_BATCH_SIZE = 500


def open_engine(mysql_url: str) -> Engine:
    """Open a sync SQLAlchemy engine against the DT MySQL source.

    The URL must use the ``mysql+pymysql://`` scheme. ``pool_pre_ping``
    catches connections that the server has timed out since the last
    use — common on a snapshot replica that's been quiet during the
    pre-cutover dry run.
    """
    return create_engine(
        mysql_url,
        pool_pre_ping=True,
        future=True,
        connect_args={"charset": "utf8mb4"},
    )


def iter_contacts(
    conn: Connection,
    *,
    watermark: datetime | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Iterator[dict[str, Any]]:
    """Yield wp_posts rows where ``post_type='contacts'``, optionally
    filtered to rows modified after ``watermark`` for delta runs.

    Yields the post row dict only; wp_postmeta is fetched separately via
    :func:`load_postmeta` for the batch.
    """
    sql = (
        "SELECT ID, post_title, post_status, post_date, post_date_gmt, "
        "post_modified, post_modified_gmt "
        "FROM wp_posts "
        "WHERE post_type = 'contacts' "
        + (" AND post_modified_gmt > :watermark " if watermark else "")
        + "ORDER BY ID ASC"
    )
    params: dict[str, Any] = {}
    if watermark is not None:
        params["watermark"] = watermark
    result = conn.execution_options(stream_results=True).execute(
        text(sql), params
    )
    batch: list[dict[str, Any]] = []
    for row in result.mappings():
        batch.append(dict(row))
        if len(batch) >= batch_size:
            yield from batch
            batch = []
    if batch:
        yield from batch


def load_postmeta(
    conn: Connection,
    post_ids: list[Any],
) -> dict[Any, list[dict[str, Any]]]:
    """Fetch all wp_postmeta rows for the given post_ids, grouped by post_id.

    Returns a dict ``{post_id: [{'meta_key': ..., 'meta_value': ...}, ...]}``.
    Pivoting to a flat dict per post is the caller's job (see
    ``mappers.contacts.pivot_postmeta``).
    """
    if not post_ids:
        return {}
    placeholders = ", ".join(f":id_{i}" for i in range(len(post_ids)))
    sql = (
        f"SELECT post_id, meta_key, meta_value FROM wp_postmeta "
        f"WHERE post_id IN ({placeholders})"
    )
    params = {f"id_{i}": pid for i, pid in enumerate(post_ids)}
    result = conn.execute(text(sql), params)
    grouped: dict[Any, list[dict[str, Any]]] = {pid: [] for pid in post_ids}
    for row in result.mappings():
        grouped.setdefault(row["post_id"], []).append(
            {"meta_key": row["meta_key"], "meta_value": row["meta_value"]}
        )
    return grouped


def iter_users(conn: Connection) -> Iterator[dict[str, Any]]:
    """Yield wp_users rows. DT installs are bounded (low hundreds of
    users) so we don't bother chunking."""
    sql = (
        "SELECT ID, user_login, user_email, user_nicename, display_name, "
        "user_status, user_registered "
        "FROM wp_users ORDER BY ID ASC"
    )
    result = conn.execute(text(sql))
    for row in result.mappings():
        yield dict(row)


def iter_comments(
    conn: Connection,
    *,
    post_ids: list[Any] | None = None,
    watermark: datetime | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Iterator[dict[str, Any]]:
    """Yield wp_comments rows for the given post_ids (or all DT contact
    comments when post_ids is None)."""
    clauses = ["1=1"]
    params: dict[str, Any] = {}
    if post_ids:
        placeholders = ", ".join(f":post_id_{i}" for i in range(len(post_ids)))
        clauses.append(f"comment_post_ID IN ({placeholders})")
        for i, pid in enumerate(post_ids):
            params[f"post_id_{i}"] = pid
    if watermark is not None:
        clauses.append("comment_date_gmt > :watermark")
        params["watermark"] = watermark
    sql = (
        "SELECT comment_ID, comment_post_ID, comment_author, "
        "comment_author_email, comment_date, comment_date_gmt, "
        "comment_content, comment_type, comment_parent, user_id, "
        "comment_agent, comment_approved "
        "FROM wp_comments "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY comment_ID ASC"
    )
    result = conn.execution_options(stream_results=True).execute(
        text(sql), params
    )
    batch: list[dict[str, Any]] = []
    for row in result.mappings():
        batch.append(dict(row))
        if len(batch) >= batch_size:
            yield from batch
            batch = []
    if batch:
        yield from batch


def iter_p2p(
    conn: Connection,
    *,
    p2p_type: str,
) -> Iterator[dict[str, Any]]:
    """Yield wp_p2p rows of a given p2p_type. DT installs may not have the
    p2p table — the orchestrator should catch :class:`OperationalError`
    and skip.
    """
    sql = (
        "SELECT p2p_id, p2p_from, p2p_to, p2p_type "
        "FROM wp_p2p WHERE p2p_type = :p2p_type ORDER BY p2p_id ASC"
    )
    result = conn.execute(text(sql), {"p2p_type": p2p_type})
    for row in result.mappings():
        yield dict(row)


def iter_activity_log(
    conn: Connection,
    *,
    watermark: datetime | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Iterator[dict[str, Any]]:
    """Yield wp_dt_activity_log field-change rows for contacts.

    Filters to ``action='field_update'`` AND ``object_type='contacts'`` —
    the rest of the table is system noise (error_log, viewed, logged_in).
    ``hist_time`` is a unix epoch int, so the watermark is compared as such.
    """
    clauses = ["action = 'field_update'", "object_type = 'contacts'"]
    params: dict[str, Any] = {}
    if watermark is not None:
        clauses.append("hist_time > :wm_unix")
        params["wm_unix"] = int(watermark.timestamp())
    sql = (
        "SELECT histid, action, object_type, object_id, user_id, hist_time, "
        "meta_key, old_value, meta_value, field_type "
        "FROM wp_dt_activity_log "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY histid ASC"
    )
    result = conn.execution_options(stream_results=True).execute(text(sql), params)
    batch: list[dict[str, Any]] = []
    for row in result.mappings():
        batch.append(dict(row))
        if len(batch) >= batch_size:
            yield from batch
            batch = []
    if batch:
        yield from batch


def fetch_max_modified(conn: Connection, table: str = "wp_posts") -> datetime | None:
    """The watermark for the next delta run is the highest post_modified_gmt
    among DT contacts at snapshot time."""
    if table == "wp_posts":
        sql = (
            "SELECT MAX(post_modified_gmt) AS max_ts "
            "FROM wp_posts WHERE post_type = 'contacts'"
        )
    elif table == "wp_comments":
        sql = "SELECT MAX(comment_date_gmt) AS max_ts FROM wp_comments"
    elif table == "wp_dt_activity_log":
        # hist_time is a unix epoch int; convert MAX to a tz-aware datetime.
        result = (
            conn.execute(
                text(
                    "SELECT MAX(hist_time) AS max_ts FROM wp_dt_activity_log "
                    "WHERE action = 'field_update' AND object_type = 'contacts'"
                )
            )
            .mappings()
            .one_or_none()
        )
        if result and result.get("max_ts"):
            return datetime.fromtimestamp(int(result["max_ts"]), tz=UTC)
        return None
    else:
        raise ValueError(f"unsupported watermark table: {table!r}")
    result = conn.execute(text(sql)).mappings().one_or_none()
    return result["max_ts"] if result and result.get("max_ts") else None


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "fetch_max_modified",
    "iter_activity_log",
    "iter_comments",
    "iter_contacts",
    "iter_p2p",
    "iter_users",
    "load_postmeta",
    "open_engine",
]
