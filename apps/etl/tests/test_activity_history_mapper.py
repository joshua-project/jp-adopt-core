"""Unit tests for DT wp_dt_activity_log → activity_log mapping."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from jp_adopt_etl.mappers.activity_history import map_activity_log_row
from jp_adopt_etl.mappers.users import LEGACY_UNKNOWN_AUTHOR_ID


def _row(**kwargs) -> dict:
    base = {
        "histid": 555,
        "action": "field_update",
        "object_type": "contacts",
        "object_id": 42,
        "user_id": 7,
        "hist_time": 1700000000,  # 2023-11-14T22:13:20Z
        "meta_key": "overall_status",
        "old_value": "new",
        "meta_value": "active",
        "field_type": "key_select",
    }
    base.update(kwargs)
    return base


def test_maps_field_update_to_rendered_activity() -> None:
    cid = uuid.uuid4()
    link = uuid.uuid4()
    out = map_activity_log_row(row=_row(), contact_id=cid, author_link_id=link)
    assert out["contact_id"] == cid
    assert out["author_id"] == str(link)
    assert out["kind"] == "field_change"
    assert out["body"] == "overall_status changed from 'new' to 'active'"
    assert out["source_system"] == "dt"
    assert out["source_id"] == "histlog:555"
    assert out["occurred_at"] == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


def test_empty_old_value_renders_as_set_to() -> None:
    out = map_activity_log_row(
        row=_row(old_value=""), contact_id=uuid.uuid4(), author_link_id=None
    )
    assert out["body"] == "overall_status set to 'active'"
    assert out["author_id"] == LEGACY_UNKNOWN_AUTHOR_ID


def test_source_metadata_preserves_change_fields() -> None:
    out = map_activity_log_row(
        row=_row(), contact_id=uuid.uuid4(), author_link_id=None
    )
    assert out["source_metadata"]["meta_key"] == "overall_status"
    assert out["source_metadata"]["old_value"] == "new"
    assert out["source_metadata"]["meta_value"] == "active"
