"""Unit tests for forms submission → intake mapper."""

from __future__ import annotations

from datetime import UTC, datetime

from jp_adopt_etl.mappers.forms import MapFailure, MapSuccess, map_submission_row


def _adoption_row(**overrides: object) -> dict:
    created = datetime(2024, 11, 15, 10, 0, tzinfo=UTC)
    submission = {
        "email": "adopter@example.com",
        "entity_name": "Test Entity",
        "contact_name": "Jane",
        "country": "United States",
        "adopter_type": "church",
        "entity_size": "31_100",
        "preferred_communication": "email",
        "mou_accepted": True,
        "newsletter_opt_in": False,
        "ministry_areas": ["evangelism"],
        "partner_entity_types": [],
        "desired_partner_info": [],
    }
    submission.update(overrides)
    return {
        "form_type": "adoption",
        "id": "11111111-1111-1111-1111-111111111111",
        "submission_id": "pga_test123",
        "created_at": created,
        "updated_at": created,
        "submission": submission,
        "fpg_selections": [
            {"people_id3": 12345, "commitment_types": ["prayer"]},
        ],
    }


def _facilitation_row(**overrides: object) -> dict:
    created = datetime(2024, 12, 1, 8, 0, tzinfo=UTC)
    submission = {
        "org_name": "Facilitator Org",
        "primary_contact_email": "fac@example.com",
        "primary_contact_name": "Bob",
        "country": "Canada",
        "works_with_fpgs": True,
        "willing_to_facilitate": True,
        "want_network_connection": False,
        "mou_status": "signed",
        "partner_with_churches": True,
        "ministry_areas": ["training"],
        "newsletter_opt_in": True,
    }
    submission.update(overrides)
    return {
        "form_type": "facilitation",
        "id": "22222222-2222-2222-2222-222222222222",
        "submission_id": "org_test456",
        "created_at": created,
        "updated_at": created,
        "submission": submission,
        "fpg_selections": [
            {
                "people_id3": 99999,
                "engagement_status": "ready",
                "facilitation_services": ["prayer"],
                "network_services": [],
            }
        ],
    }


def test_map_adoption_happy_path() -> None:
    result = map_submission_row(_adoption_row())
    assert isinstance(result, MapSuccess)
    assert result.form_type == "adoption"
    assert result.payload.email == "adopter@example.com"
    assert result.created_at.year == 2024
    assert len(result.payload.fpg_selections) == 1


def test_map_facilitation_happy_path() -> None:
    result = map_submission_row(_facilitation_row())
    assert isinstance(result, MapSuccess)
    assert result.form_type == "facilitation"
    assert result.payload.organization_name == "Facilitator Org"


def test_map_missing_email() -> None:
    row = _adoption_row()
    row["submission"] = {k: v for k, v in row["submission"].items() if k != "email"}
    result = map_submission_row(row)
    assert isinstance(result, MapFailure)
    assert "missing email" in result.reason


def test_map_unknown_form_type() -> None:
    row = _adoption_row()
    row["form_type"] = "unknown"
    result = map_submission_row(row)
    assert isinstance(result, MapFailure)
    assert result.reason == "unknown_form_type"


def test_map_bad_origin_still_website() -> None:
    result = map_submission_row(_adoption_row())
    assert isinstance(result, MapSuccess)
    assert result.payload.origin == "website"


def test_created_at_preserved() -> None:
    result = map_submission_row(_facilitation_row())
    assert isinstance(result, MapSuccess)
    assert result.created_at == datetime(2024, 12, 1, 8, 0, tzinfo=UTC)
