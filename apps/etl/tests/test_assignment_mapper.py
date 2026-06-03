"""Unit tests for parsing DT ``assigned_to`` → wp_user_id."""

from __future__ import annotations

from jp_adopt_etl.mappers.assignment import parse_assigned_user_id


def test_parses_user_prefix() -> None:
    assert parse_assigned_user_id("user-2") == "2"


def test_unassigned_variants_return_none() -> None:
    assert parse_assigned_user_id("user-") is None
    assert parse_assigned_user_id("") is None
    assert parse_assigned_user_id(None) is None
    assert parse_assigned_user_id("0") is None


def test_non_numeric_or_unexpected_shape_returns_none() -> None:
    assert parse_assigned_user_id("user-abc") is None
    assert parse_assigned_user_id("2") is None  # missing 'user-' prefix
