"""Pure-unit tests for Track B reconciliation logic — no DB, no MySQL.

Covers the operator-mapping parsing and the handle-aggregation diagnostics,
which are the load-bearing pure pieces of track_b_assignments.
"""

from __future__ import annotations

import json

import pytest
from jp_adopt_etl.reconcile.track_b_assignments import (
    MappedSubject,
    SubjectMapping,
)


def test_mapping_from_flat_string_spec():
    m = SubjectMapping.from_dict({"user-12": "oid-abc", "user-7": "oid-def"})
    assert m.get("user-12").subject_id == "oid-abc"
    assert m.get("user-12").wp_user_id == "12"
    assert m.get("user-7").subject_id == "oid-def"
    assert m.get("user-999") is None


def test_mapping_from_object_spec_with_email_and_auth_link():
    m = SubjectMapping.from_dict(
        {
            "user-3": {
                "subject": "oid-xyz",
                "email": "Staff@Example.com",
                "display_name": "Staff Person",
                "link_auth_identity": True,
            }
        }
    )
    mapped = m.get("user-3")
    assert mapped.subject_id == "oid-xyz"
    assert mapped.email == "Staff@Example.com"
    assert mapped.display_name == "Staff Person"
    assert mapped.link_auth_identity is True


def test_mapping_accepts_oid_alias():
    m = SubjectMapping.from_dict({"user-5": {"oid": "oid-from-oid-key"}})
    assert m.get("user-5").subject_id == "oid-from-oid-key"


def test_mapping_object_requires_subject():
    with pytest.raises(ValueError):
        SubjectMapping.from_dict({"user-1": {"email": "no-subject@x.dev"}})


def test_mapping_rejects_non_str_non_dict_spec():
    with pytest.raises(ValueError):
        SubjectMapping.from_dict({"user-1": 12345})


def test_mapping_from_file_supports_wrapped_and_flat(tmp_path):
    flat = tmp_path / "flat.json"
    flat.write_text(json.dumps({"user-2": "oid-flat"}))
    assert SubjectMapping.from_file(flat).get("user-2").subject_id == "oid-flat"

    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"mapping": {"user-2": "oid-wrapped"}}))
    assert SubjectMapping.from_file(wrapped).get("user-2").subject_id == "oid-wrapped"


def test_mapped_subject_wp_user_id_handles_garbage():
    assert MappedSubject(handle="not-a-handle", subject_id="s").wp_user_id is None
    assert MappedSubject(handle="user-0", subject_id="s").wp_user_id is None
    assert MappedSubject(handle="user-42", subject_id="s").wp_user_id == "42"
