"""DT ``wp_dt_activity_log`` (field-change history) → ``ActivityLog`` kwargs.

Only ``action='field_update'`` rows on ``object_type='contacts'`` are migrated
(the rest of the table is system noise — error_log, viewed, logged_in). Each
row becomes a one-line ``kind='field_change'`` activity entry. The orchestrator
applies the action/object_type filter when reading; this mapper renders a row
it is handed. See .dt-inspection/ASSESSMENT.md / decisions.md.

Pure function — no I/O.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from jp_adopt_etl.mappers.users import LEGACY_UNKNOWN_AUTHOR_ID


def _render_body(meta_key: str, old_value: str, new_value: str) -> str:
    if old_value:
        return f"{meta_key} changed from '{old_value}' to '{new_value}'"
    return f"{meta_key} set to '{new_value}'"


def map_activity_log_row(
    *,
    row: dict[str, Any],
    contact_id: uuid.UUID,
    author_link_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Translate one wp_dt_activity_log row into ActivityLog kwargs.

    ``source_id`` is prefixed ``histlog:`` so it can never collide with a
    wp_comments-derived activity_log row's source_id.
    """
    histid = str(row["histid"])
    meta_key = str(row.get("meta_key") or "field")
    old_value = str(row.get("old_value") or "")
    new_value = str(row.get("meta_value") or "")

    hist_time = row.get("hist_time") or 0
    occurred_at = datetime.fromtimestamp(int(hist_time), tz=UTC)

    metadata = {
        "action": row.get("action"),
        "object_type": row.get("object_type"),
        "meta_key": row.get("meta_key"),
        "old_value": row.get("old_value"),
        "meta_value": row.get("meta_value"),
        "field_type": row.get("field_type"),
    }
    metadata = {k: v for k, v in metadata.items() if v is not None and v != ""}

    author_id = str(author_link_id) if author_link_id else LEGACY_UNKNOWN_AUTHOR_ID
    return {
        "contact_id": contact_id,
        "author_id": author_id,
        "body": _render_body(meta_key, old_value, new_value),
        "kind": "field_change",
        "parent_id": None,
        "source_system": "dt",
        "source_id": f"histlog:{histid}",
        "source_metadata": metadata or None,
        "occurred_at": occurred_at,
    }


__all__ = ["map_activity_log_row"]
