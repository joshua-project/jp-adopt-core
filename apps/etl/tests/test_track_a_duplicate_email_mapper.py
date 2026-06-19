"""Pure-unit tests for Track A duplicate_email reconciliation logic.

No DB, no MySQL — just the name-similarity heuristic and review-list
rendering. The DB-touching plan/apply paths are covered in
``test_track_a_reconcile_integration.py``.
"""

from __future__ import annotations

import json
import uuid

from jp_adopt_etl.reconcile.track_a_duplicate_email import (
    MergePlan,
    ReconcileResult,
    names_look_like_same_person,
    write_review_list,
)


class TestNameSimilarity:
    def test_exact_match_is_same_person(self):
        assert names_look_like_same_person("Jane Doe", "Jane Doe")

    def test_case_and_whitespace_insensitive(self):
        assert names_look_like_same_person("  jane   DOE ", "Jane Doe")

    def test_middle_name_subset_is_same_person(self):
        assert names_look_like_same_person("Jane Doe", "Jane M. Doe")

    def test_shared_surname_is_same_person(self):
        # 'John Smith' vs 'Jonathan Smith' share surname token 'smith'.
        assert names_look_like_same_person("John Smith", "Jonathan Smith")

    def test_blank_name_is_no_signal_same_person(self):
        # Missing DT title must not manufacture review noise.
        assert names_look_like_same_person(None, "Jane Doe")
        assert names_look_like_same_person("", "Jane Doe")

    def test_unrelated_names_are_ambiguous(self):
        # Shared family inbox: same email, clearly different people.
        assert not names_look_like_same_person("Jane Doe", "Bob Jones")

    def test_punctuation_does_not_block_match(self):
        assert names_look_like_same_person("O'Brien, Mary", "Mary OBrien")


class TestReviewList:
    def _result(self) -> ReconcileResult:
        r = ReconcileResult()
        tid = uuid.uuid4()
        r.planned.append(
            MergePlan(
                source_id="9201",
                email_normalized="shared@x.dev",
                target_contact_id=tid,
                target_display_name="Bob Jones",
                dt_display_name="Jane Doe",
                status="review",
                reason="names differ",
            )
        )
        # A merge plan should NOT appear in the review list.
        r.planned.append(
            MergePlan(
                source_id="9202",
                email_normalized="same@x.dev",
                status="merge",
            )
        )
        return r

    def test_json_review_list_only_contains_review_rows(self, tmp_path):
        out = tmp_path / "review.json"
        n = write_review_list(self._result(), str(out))
        assert n == 1
        rows = json.loads(out.read_text())
        assert len(rows) == 1
        assert rows[0]["source_id"] == "9201"
        assert rows[0]["dt_display_name"] == "Jane Doe"
        assert rows[0]["local_display_name"] == "Bob Jones"

    def test_csv_review_list_has_header(self, tmp_path):
        out = tmp_path / "review.csv"
        n = write_review_list(self._result(), str(out))
        assert n == 1
        text = out.read_text()
        lines = text.strip().splitlines()
        assert lines[0].startswith("source_id,email_normalized")
        assert "9201" in lines[1]


class TestResultCounts:
    def test_counts_partition_correctly(self):
        r = ReconcileResult()
        r.planned.append(MergePlan(source_id="1", email_normalized="a", status="merge"))
        r.planned.append(MergePlan(source_id="2", email_normalized="b", status="merge"))
        r.planned.append(
            MergePlan(source_id="3", email_normalized="c", status="review")
        )
        r.planned.append(
            MergePlan(
                source_id="4", email_normalized="d", status="skip_missing_target"
            )
        )
        c = r.counts()
        assert c["rows_in"] == 4
        assert c["rows_out_inserted"] == 2
        assert c["rows_in_review"] == 1
        # skipped (1) + review (1) both count as not-written.
        assert c["rows_out_skipped"] == 2
