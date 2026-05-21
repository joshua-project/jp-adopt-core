"""U9 status mapper unit tests."""

from __future__ import annotations

import pytest

from jp_adopt_etl.mappers.status import (
    UNKNOWN_SENTINEL,
    UnmappedStatusError,
    map_adopter_status,
    map_facilitator_status,
)

# ─── adopter mapping (per plan / launch-readiness table) ──────────────────


@pytest.mark.parametrize(
    "source,expected",
    [
        ("draft", "draft"),
        ("new", "new"),
        ("new_inquiry", "new"),  # spike-era artifact
        ("contacted", "new"),
        ("engaged", "contacted"),
        ("matched", "matched"),
        ("active", "matched"),
        ("inactive", "do_not_engage"),
    ],
)
def test_adopter_status_known_values(source: str, expected: str) -> None:
    assert map_adopter_status(source, mode="dry_run") == expected
    assert map_adopter_status(source, mode="production") == expected


def test_adopter_status_handles_none_and_empty() -> None:
    assert map_adopter_status(None, mode="dry_run") is None
    assert map_adopter_status("", mode="production") is None
    assert map_adopter_status("   ", mode="production") is None


def test_adopter_status_is_case_insensitive() -> None:
    assert map_adopter_status("NEW", mode="production") == "new"
    assert map_adopter_status("  Engaged  ", mode="dry_run") == "contacted"


def test_adopter_status_dry_run_raises_on_unmapped() -> None:
    with pytest.raises(UnmappedStatusError) as exc_info:
        map_adopter_status("weird_unmapped_value", mode="dry_run")
    assert exc_info.value.party_kind == "adopter"
    assert exc_info.value.source_value == "weird_unmapped_value"


def test_adopter_status_production_maps_unknown() -> None:
    assert (
        map_adopter_status("weird_unmapped_value", mode="production")
        == UNKNOWN_SENTINEL
    )


# ─── facilitator mapping (separate target enum) ───────────────────────────


@pytest.mark.parametrize(
    "source,expected",
    [
        ("draft", "draft"),
        ("new", "new"),
        ("contacted", "new"),
        ("engaged", "not_ready"),
        ("matched", "ready"),
        ("active", "ready"),
        ("inactive", "do_not_engage"),
    ],
)
def test_facilitator_status_known_values(source: str, expected: str) -> None:
    assert map_facilitator_status(source, mode="dry_run") == expected
    assert map_facilitator_status(source, mode="production") == expected


def test_facilitator_status_dry_run_raises_on_unmapped() -> None:
    with pytest.raises(UnmappedStatusError) as exc_info:
        map_facilitator_status("weird_value", mode="dry_run")
    assert exc_info.value.party_kind == "facilitator"


def test_facilitator_status_production_maps_unknown() -> None:
    assert (
        map_facilitator_status("weird_value", mode="production") == UNKNOWN_SENTINEL
    )


def test_adopter_and_facilitator_diverge_on_same_source() -> None:
    """The DT 'engaged' value goes to different target states depending on
    the contact's party_kind — proves the two mappers don't collapse."""
    assert map_adopter_status("engaged", mode="dry_run") == "contacted"
    assert map_facilitator_status("engaged", mode="dry_run") == "not_ready"
