"""Pure-unit tests for the Track A DT-authoritative merge rules.

No DB, no MySQL — these exercise the field-overwrite, consent
most-restrictive, and interest-union rules in isolation.
"""

from __future__ import annotations

from jp_adopt_etl.reconcile.track_a_merge import (
    consent_most_restrictive,
    interests_to_add,
    merge_descriptive,
)


def test_dt_overwrites_nonempty_core_value():
    out = merge_descriptive(
        core={"phone": "111", "origin": "forms"},
        dt={"phone": "222", "origin": None},
    )
    assert out == {"phone": "222"}  # DT wins where DT has a value


def test_keeps_core_where_dt_empty():
    out = merge_descriptive(
        core={"country_code": "US"},
        dt={"country_code": None},
    )
    assert out == {}  # nothing to change


def test_empty_string_dt_value_is_skipped():
    out = merge_descriptive(core={"phone": "111"}, dt={"phone": ""})
    assert out == {}


def test_unchanged_value_is_not_emitted():
    out = merge_descriptive(core={"phone": "111"}, dt={"phone": "111"})
    assert out == {}


def test_consent_optout_in_core_wins():
    # core opted out of 'email' => DT 'email' consent must NOT re-enable it
    decision = consent_most_restrictive(core_optouts={"email"}, dt_optouts=set())
    assert "email" in decision.effective_optouts
    assert decision.dt_consents_to_add == set()  # blocked by core opt-out


def test_consent_dt_optout_propagates():
    decision = consent_most_restrictive(core_optouts=set(), dt_optouts={"email"})
    assert "email" in decision.effective_optouts


def test_interests_union_adds_only_missing():
    add = interests_to_add(core_keys={"pid-1"}, dt_keys={"pid-1", "pid-2"})
    assert add == {"pid-2"}
