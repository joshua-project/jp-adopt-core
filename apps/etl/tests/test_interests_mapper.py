"""Unit tests for parsing DT ``fpg_submission_data`` → AdopterInterest kwargs."""

from __future__ import annotations

from jp_adopt_etl.mappers.interests import parse_fpg_submission_data

RAW = (
    '[{"peopleId3":10376,"engagementStatus":"ready","canFacilitate":true,'
    '"facilitationServices":["prayer_updates","financial"],"networkServices":[],'
    '"commitmentTypes":["pray"]},'
    '{"peopleId3":10379,"engagementStatus":"potential","canFacilitate":false,'
    '"facilitationServices":[],"networkServices":["referrals"],"commitmentTypes":[]}]'
)


def test_parse_maps_camel_to_interest_kwargs() -> None:
    rows = parse_fpg_submission_data(RAW)
    assert rows[0] == {
        "people_id3": "10376",
        "engagement_status": "ready",
        "facilitation_services": ["prayer_updates", "financial"],
        "network_services": [],
        "commitment_types": ["pray"],
        "commitment_level": None,
        "notes": None,
    }
    assert rows[1]["people_id3"] == "10379"
    assert rows[1]["engagement_status"] == "potential"
    assert rows[1]["network_services"] == ["referrals"]


def test_parse_handles_empty_or_garbage() -> None:
    assert parse_fpg_submission_data("") == []
    assert parse_fpg_submission_data(None) == []
    assert parse_fpg_submission_data("not json") == []
    assert parse_fpg_submission_data("{}") == []  # object, not a list
    assert parse_fpg_submission_data("[]") == []


def test_parse_skips_elements_without_people_id3() -> None:
    rows = parse_fpg_submission_data('[{"engagementStatus":"ready"},{"peopleId3":42}]')
    assert len(rows) == 1
    assert rows[0]["people_id3"] == "42"
