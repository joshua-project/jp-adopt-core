"""CLI parsing tests for forms-etl."""

from __future__ import annotations

from datetime import UTC, datetime

from jp_adopt_etl.forms_orchestrator import _parse_watermark


def test_parse_watermark_naive_assumes_utc() -> None:
    assert _parse_watermark("2024-06-01T12:00:00") == datetime(
        2024, 6, 1, 12, 0, 0, tzinfo=UTC
    )


def test_parse_watermark_offset_converts_to_utc() -> None:
    from datetime import timezone, timedelta

    eastern = timezone(timedelta(hours=-5))
    parsed = _parse_watermark("2024-06-01T12:00:00-05:00")
    assert parsed == datetime(2024, 6, 1, 17, 0, 0, tzinfo=UTC)
    assert parsed.tzinfo == UTC
    # Sanity: input instant in Eastern equals parsed UTC instant
    assert datetime(2024, 6, 1, 12, 0, 0, tzinfo=eastern).astimezone(UTC) == parsed
