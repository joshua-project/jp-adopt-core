"""Unit tests for DT comm-channel extraction.

DT stores each email/phone as a ``contact_email_<hash>`` / ``contact_phone_<hash>``
postmeta key, paired with a ``<key>_details`` php-serialized blob carrying
``verified`` (and other flags). See .dt-inspection/ASSESSMENT.md.
"""

from __future__ import annotations

from jp_adopt_etl.mappers.channels import extract_comm_channels


def test_extract_primary_email_prefers_verified() -> None:
    meta = {
        "contact_email_047": "unverified@x.dev",
        "contact_email_047_details": 'a:1:{s:8:"verified";b:0;}',
        "contact_email_9aa": "verified@x.dev",
        "contact_email_9aa_details": 'a:1:{s:8:"verified";b:1;}',
    }
    out = extract_comm_channels(meta)
    assert out["email"] == "verified@x.dev"
    assert out["extra_emails"] == ["unverified@x.dev"]


def test_extract_phone() -> None:
    meta = {
        "contact_phone_285": "+1 555 0100",
        "contact_phone_285_details": 'a:1:{s:8:"verified";b:0;}',
    }
    out = extract_comm_channels(meta)
    assert out["phone"] == "+1 555 0100"


def test_single_email_no_details() -> None:
    out = extract_comm_channels({"contact_email_047": "a@x.dev"})
    assert out["email"] == "a@x.dev"
    assert out["extra_emails"] == []
    assert out["phone"] is None


def test_ignores_details_keys_and_non_channel_keys() -> None:
    meta = {
        "contact_email_047_details": 'a:1:{s:8:"verified";b:1;}',
        "name": "Alice",
        "overall_status": "new",
    }
    out = extract_comm_channels(meta)
    assert out["email"] is None
    assert out["phone"] is None


def test_empty_values_skipped() -> None:
    out = extract_comm_channels({"contact_email_047": "", "contact_phone_285": "  "})
    assert out["email"] is None
    assert out["phone"] is None
