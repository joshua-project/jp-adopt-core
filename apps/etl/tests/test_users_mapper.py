"""U9 users mapper unit tests."""

from __future__ import annotations

from jp_adopt_etl.mappers.users import LEGACY_UNKNOWN_AUTHOR_ID, map_user


def test_map_user_basic() -> None:
    kwargs = map_user(
        {
            "ID": 5,
            "user_email": " Alice@Example.com ",
            "display_name": "Alice Smith",
            "user_login": "alice",
            "user_status": 0,
        }
    )
    assert kwargs["dt_user_id"] == "5"
    assert kwargs["email"] == "Alice@Example.com"
    assert kwargs["email_normalized"] == "alice@example.com"
    assert kwargs["display_name"] == "Alice Smith"
    assert kwargs["status"] == "active"
    assert kwargs["source_system"] == "dt"
    assert kwargs["b2c_subject_id"] is None


def test_map_user_inactive_status() -> None:
    kwargs = map_user(
        {
            "ID": 6,
            "user_email": "bob@example.com",
            "user_login": "bob",
            "user_status": 2,  # non-zero
        }
    )
    assert kwargs["status"] == "inactive"


def test_map_user_falls_back_to_login_for_display_name() -> None:
    kwargs = map_user(
        {
            "ID": 7,
            "user_email": "c@example.com",
            "user_login": "carol",
            "display_name": None,
        }
    )
    assert kwargs["display_name"] == "carol"


def test_map_user_handles_missing_email() -> None:
    """Some DT installs have users without emails (deleted, system); the
    caller decides whether to skip them. The mapper just returns empties."""
    kwargs = map_user({"ID": 8, "user_email": "", "user_login": "user8"})
    assert kwargs["email"] == ""
    assert kwargs["email_normalized"] == ""


def test_legacy_unknown_sentinel_is_distinct() -> None:
    """Defensive: any real wp_users-derived id would be a UUID stringified,
    never the system sentinel."""
    assert LEGACY_UNKNOWN_AUTHOR_ID.startswith("system:")
