"""DT ``wp_comments`` (+ wp_dt_activity_log when present) → ``ActivityLog``
ORM kwargs.

Threading is preserved via ``parent_id`` self-FK. Authors resolve through
``staff_identity_link``: when the wp_comments row's ``user_id`` matches a
StaffIdentityLink row, the activity_log's author_id is the link's UUID
as a string. Missing wp_users → ``system:dt_legacy_unknown`` sentinel.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from jp_adopt_etl.mappers.users import LEGACY_UNKNOWN_AUTHOR_ID


def _coerce_datetime(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=UTC)
        return raw.astimezone(UTC)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def map_comment(
    *,
    comment_row: dict[str, Any],
    contact_id: uuid.UUID,
    author_link_id: uuid.UUID | None,
    parent_activity_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Translate one wp_comments row into ActivityLog kwargs.

    ``contact_id`` is the new Postgres Contact UUID (resolved by the
    orchestrator via the source_id → contact lookup).
    ``author_link_id`` is the new StaffIdentityLink UUID for this
    comment's wp_comments.user_id; ``None`` triggers the legacy-unknown
    sentinel.
    ``parent_activity_id`` is the resolved parent activity_log UUID when
    the orchestrator has already imported the parent (two-pass strategy);
    leave ``None`` for the first pass and update via a second-pass query
    using ``source_metadata.parent_source_id``.
    """
    comment_id = str(comment_row["comment_ID"])
    body = (comment_row.get("comment_content") or "").strip()
    occurred_at = (
        _coerce_datetime(comment_row.get("comment_date_gmt"))
        or _coerce_datetime(comment_row.get("comment_date"))
        or datetime.now(UTC)
    )
    # comment_type is empty string for plain WP comments and named for
    # plugin-emitted activity (e.g. 'duplicate', 'status_change').
    raw_kind = comment_row.get("comment_type") or None
    kind = raw_kind.strip().lower() if raw_kind else None

    parent_source_id = comment_row.get("comment_parent") or None
    parent_metadata: dict[str, Any] = {}
    if parent_source_id and int(parent_source_id) != 0:
        # Preserve the source parent ID so a second pass can resolve the
        # actual parent activity_log UUID after all comments are imported.
        parent_metadata["parent_source_id"] = str(parent_source_id)

    author_id: str
    if author_link_id is not None:
        author_id = str(author_link_id)
    else:
        author_id = LEGACY_UNKNOWN_AUTHOR_ID

    metadata = {
        "comment_agent": comment_row.get("comment_agent"),
        "comment_author_email": comment_row.get("comment_author_email"),
        "comment_approved": comment_row.get("comment_approved"),
        **parent_metadata,
    }
    metadata = {k: v for k, v in metadata.items() if v is not None and v != ""}

    return {
        "contact_id": contact_id,
        "author_id": author_id,
        "body": body,
        "kind": kind,
        "parent_id": parent_activity_id,
        "source_system": "dt",
        "source_id": comment_id,
        "source_metadata": metadata or None,
        "occurred_at": occurred_at,
    }


__all__ = ["map_comment"]
