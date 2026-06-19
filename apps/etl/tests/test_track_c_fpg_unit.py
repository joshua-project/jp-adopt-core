"""Pure-unit tests for Track C fpg_not_found reconcile helpers (no DB)."""

from __future__ import annotations

from jp_adopt_etl.reconcile.track_c_fpg import FpgConflict, ReconcileReport


def test_from_row_splits_source_id_and_prefers_source_value() -> None:
    c = FpgConflict.from_row("9001:18421", {"people_id3": "18421"})
    assert c.post_id == "9001"
    assert c.people_id3 == "18421"
    assert c.source_id == "9001:18421"


def test_from_row_falls_back_to_source_id_tail_when_source_value_missing() -> None:
    c = FpgConflict.from_row("9002:77777", None)
    assert c.post_id == "9002"
    assert c.people_id3 == "77777"


def test_from_row_uses_last_colon_so_post_id_with_colon_is_safe() -> None:
    # Defensive: rpartition on the LAST ':' keeps the people_id3 intact.
    c = FpgConflict.from_row("weird:9003:18421", {"people_id3": "18421"})
    assert c.people_id3 == "18421"
    assert c.post_id == "weird:9003"


def test_from_row_handles_missing_colon_gracefully() -> None:
    c = FpgConflict.from_row("9004", {"people_id3": "18421"})
    assert c.post_id == "9004"
    assert c.people_id3 == "18421"


def test_report_as_dict_dedupes_stale_people_ids() -> None:
    report = ReconcileReport(mode="dry_run")
    report.conflicts_seen = 3
    report.resolved.append(FpgConflict("9001:18421", "9001", "18421"))
    report.still_stale.append(FpgConflict("9002:99999", "9002", "99999"))
    report.still_stale.append(FpgConflict("9003:99999", "9003", "99999"))
    d = report.as_dict()
    assert d["mode"] == "dry_run"
    assert d["resolved"] == ["9001:18421"]
    assert d["still_stale"] == ["99999"]  # deduped
    assert d["conflicts_seen"] == 3
