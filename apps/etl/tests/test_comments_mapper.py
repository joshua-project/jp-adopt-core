"""U9 comments mapper unit tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from jp_adopt_etl.mappers.comments import map_comment
from jp_adopt_etl.mappers.users import LEGACY_UNKNOWN_AUTHOR_ID


def _row(**kwargs) -> dict:
    base = {
        "comment_ID": 100,
        "comment_post_ID": 1,
        "comment_author": "Alice",
        "comment_author_email": "alice@example.com",
        "comment_date": None,
        "comment_date_gmt": "2025-12-01T10:00:00",
        "comment_content": "Followed up by phone.",
        "comment_type": "",
        "comment_parent": 0,
        "user_id": 42,
        "comment_agent": "Mozilla/5.0",
        "comment_approved": "1",
    }
    base.update(kwargs)
    return base


def test_map_comment_basic_with_resolved_author() -> None:
    contact_id = uuid.uuid4()
    link_id = uuid.uuid4()
    kwargs = map_comment(
        comment_row=_row(),
        contact_id=contact_id,
        author_link_id=link_id,
    )
    assert kwargs["contact_id"] == contact_id
    assert kwargs["author_id"] == str(link_id)
    assert kwargs["body"] == "Followed up by phone."
    assert kwargs["source_system"] == "dt"
    assert kwargs["source_id"] == "100"
    assert kwargs["kind"] is None  # empty string normalized to None
    assert isinstance(kwargs["occurred_at"], datetime)
    assert kwargs["occurred_at"].tzinfo is UTC


def test_map_comment_legacy_unknown_author_when_link_is_none() -> None:
    kwargs = map_comment(
        comment_row=_row(user_id=0),
        contact_id=uuid.uuid4(),
        author_link_id=None,
    )
    assert kwargs["author_id"] == LEGACY_UNKNOWN_AUTHOR_ID


def test_map_comment_preserves_kind() -> None:
    kwargs = map_comment(
        comment_row=_row(comment_type="status_change"),
        contact_id=uuid.uuid4(),
        author_link_id=uuid.uuid4(),
    )
    assert kwargs["kind"] == "status_change"


def test_map_comment_records_parent_source_id_in_metadata() -> None:
    kwargs = map_comment(
        comment_row=_row(comment_parent=99),
        contact_id=uuid.uuid4(),
        author_link_id=uuid.uuid4(),
    )
    assert kwargs["source_metadata"] is not None
    assert kwargs["source_metadata"]["parent_source_id"] == "99"


def test_map_comment_drops_empty_metadata_fields() -> None:
    kwargs = map_comment(
        comment_row=_row(comment_agent="", comment_author_email=None),
        contact_id=uuid.uuid4(),
        author_link_id=uuid.uuid4(),
    )
    # All metadata fields blank → source_metadata is None, not an empty dict.
    assert kwargs["source_metadata"] is None or "comment_agent" not in kwargs[
        "source_metadata"
    ]


def test_map_comment_coerces_naive_datetime_to_utc() -> None:
    naive = datetime(2025, 12, 1, 10, 0, 0)
    kwargs = map_comment(
        comment_row=_row(comment_date_gmt=naive),
        contact_id=uuid.uuid4(),
        author_link_id=uuid.uuid4(),
    )
    assert kwargs["occurred_at"].tzinfo is UTC
