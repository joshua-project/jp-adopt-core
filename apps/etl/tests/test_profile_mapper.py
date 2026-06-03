"""Unit tests for the DT → contact_profile mapper."""

from __future__ import annotations

import phpserialize

from jp_adopt_etl.mappers.profile import map_contact_profile


def _ser(values: list[str]) -> str:
    return phpserialize.dumps(values).decode("utf-8")


def test_returns_none_when_empty() -> None:
    assert map_contact_profile({}) is None
    assert map_contact_profile({"sub_type": "adopter"}) is None


def test_maps_direct_string_fields() -> None:
    out = map_contact_profile(
        {
            "website": "https://x.org",
            "primary_contact_name": "Alice",
            "form_state_region": "TX",
            "additional_notes": "hello",
        }
    )
    assert out["website"] == "https://x.org"
    assert out["primary_contact_name"] == "Alice"
    assert out["form_state_region"] == "TX"
    assert out["additional_notes"] == "hello"


def test_maps_multi_select_php_array_to_list() -> None:
    out = map_contact_profile({"ministry_areas": _ser(["prayer", "training"])})
    assert out["ministry_areas"] == ["prayer", "training"]


def test_maps_booleans() -> None:
    out = map_contact_profile(
        {"has_doctrinal_distinctives": "1", "works_with_fpgs": "0"}
    )
    assert out["has_doctrinal_distinctives"] is True
    assert out["works_with_fpgs"] is False


def test_maps_engagement_score_int() -> None:
    assert map_contact_profile({"engagement_score": "42"})["engagement_score"] == 42
    # non-numeric → omitted (website filler keeps the result a dict)
    out = map_contact_profile({"engagement_score": "x", "website": "x"})
    assert "engagement_score" not in out


def test_maps_dates() -> None:
    out = map_contact_profile({"commitment_date": "2026-04-14"})
    assert out["commitment_date"].isoformat() == "2026-04-14"


def test_enum_values_within_domain_pass_through() -> None:
    out = map_contact_profile(
        {
            "entity_size": "101_500",
            "adopter_type": "organization",
            "mou_status": "signed",
            "preferred_communication": "email",
        }
    )
    assert out["entity_size"] == "101_500"
    assert out["adopter_type"] == "organization"
    assert out["mou_status"] == "signed"
    assert out["preferred_communication"] == "email"


def test_out_of_domain_enum_clamped_to_none() -> None:
    # An unexpected DT value must not violate the CHECK constraint downstream.
    out = map_contact_profile({"entity_size": "wildly_invalid", "website": "x"})
    assert out.get("entity_size") is None
