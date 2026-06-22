"""Pure-unit tests for the Track A DT-authoritative merge rules.

No DB, no MySQL — these exercise the field-overwrite, consent
most-restrictive, and interest-union rules in isolation.
"""

from __future__ import annotations

from jp_adopt_etl.reconcile.track_a_merge import (
    consent_most_restrictive,
    interests_to_add,
    is_real_name,
    merge_descriptive,
    pick_winner,
    resolve_display_name,
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


# ─── multi-collision recommended-keeper: is_real_name + pick_winner ──────────


def test_is_real_name_true_for_genuine_name():
    assert is_real_name("Suranjan Sim", "suranjansim@example.com") is True


def test_is_real_name_false_for_full_email():
    assert is_real_name("suranjansim@example.com", "suranjansim@example.com") is False


def test_is_real_name_false_for_email_local_part():
    assert is_real_name("suranjansim", "suranjansim@example.com") is False


def test_is_real_name_false_for_empty():
    assert is_real_name("", "x@example.com") is False
    assert is_real_name(None, "x@example.com") is False


def test_is_real_name_case_and_whitespace_insensitive():
    # Surrounding whitespace + different case still detected as the email.
    assert is_real_name("  SuranjanSim@Example.com  ", "suranjansim@example.com") is False
    assert is_real_name("  SURANJANSIM  ", "suranjansim@example.com") is False


def test_is_real_name_true_when_no_email():
    # No email to compare against => any non-empty name is "real".
    assert is_real_name("Jane Doe", None) is True


def test_pick_winner_real_name_beats_more_filled_email_name():
    # The suranjansim bug: the email-named record has MORE fields (15) but
    # the real-named record (13) is the correct entity and must win.
    candidates = [
        {
            "source_id": "100",
            "name": "suranjansim@example.com",
            "email": "suranjansim@example.com",
            "filled": 15,
            "created": "2020-01-01",
        },
        {
            "source_id": "200",
            "name": "Suranjan Sim",
            "email": "suranjansim@example.com",
            "filled": 13,
            "created": "2019-01-01",
        },
    ]
    assert pick_winner(candidates)["source_id"] == "200"


def test_pick_winner_filled_tiebreak_among_real_names():
    candidates = [
        {"source_id": "1", "name": "Jane Doe", "email": "j@x.com",
         "filled": 5, "created": "2020-01-01"},
        {"source_id": "2", "name": "Jane M Doe", "email": "j@x.com",
         "filled": 9, "created": "2019-01-01"},
    ]
    assert pick_winner(candidates)["source_id"] == "2"


def test_pick_winner_created_tiebreak_among_real_names():
    candidates = [
        {"source_id": "1", "name": "Jane Doe", "email": "j@x.com",
         "filled": 7, "created": "2019-06-01"},
        {"source_id": "2", "name": "Jane Doe", "email": "j@x.com",
         "filled": 7, "created": "2021-06-01"},
    ]
    assert pick_winner(candidates)["source_id"] == "2"


def test_pick_winner_stable_by_source_id():
    candidates = [
        {"source_id": "200", "name": "Jane Doe", "email": "j@x.com",
         "filled": 7, "created": "2020-01-01"},
        {"source_id": "100", "name": "Jane Doe", "email": "j@x.com",
         "filled": 7, "created": "2020-01-01"},
    ]
    assert pick_winner(candidates)["source_id"] == "100"


# ─── name-aware display_name merge: resolve_display_name ──────────────────────


def test_resolve_display_name_keeps_core_when_dt_is_email():
    # DT display_name IS the email (fallback name); core is a real name =>
    # keep the core name, do not overwrite. Returns None for "no change".
    out = resolve_display_name(
        core_name="John Auer",
        dt_name="crossroads1947@yahoo.com",
        email="crossroads1947@yahoo.com",
    )
    assert out is None


def test_resolve_display_name_dt_real_name_wins_over_core_email():
    # DT is a real name, core is the email => DT wins (overwrite).
    out = resolve_display_name(
        core_name="lolla@example.com",
        dt_name="Lolla Cronje",
        email="lolla@example.com",
    )
    assert out == "Lolla Cronje"


def test_resolve_display_name_both_real_dt_wins():
    # Both real but different => DT-authoritative default, DT wins.
    out = resolve_display_name(
        core_name="John Auer",
        dt_name="Jonathan Auer",
        email="auer@example.com",
    )
    assert out == "Jonathan Auer"


def test_resolve_display_name_neither_real_dt_wins():
    # Neither is a real name (both email-as-name, but different) => DT wins
    # (authoritative default).
    out = resolve_display_name(
        core_name="x",  # email local-part fallback
        dt_name="x@example.com",  # full-email fallback
        email="x@example.com",
    )
    assert out == "x@example.com"


def test_resolve_display_name_dt_empty_is_no_change():
    # DT has no name to write => no change (keep core).
    assert resolve_display_name(
        core_name="John Auer", dt_name=None, email="x@example.com"
    ) is None
    assert resolve_display_name(
        core_name="John Auer", dt_name="", email="x@example.com"
    ) is None


def test_resolve_display_name_no_change_when_equal():
    assert resolve_display_name(
        core_name="John Auer", dt_name="John Auer", email="x@example.com"
    ) is None
