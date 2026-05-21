"""U9 contacts mapper unit tests.

Covers wp_postmeta pivot, phpserialize behavior, status mapping
integration, and edge cases (missing title, empty meta, weird types).
"""

from __future__ import annotations

import phpserialize
import pytest

from jp_adopt_etl.mappers.contacts import (
    META_KEY_ADOPTER_STATUS,
    META_KEY_COUNTRY_CODE,
    META_KEY_DISPLAY_NAME,
    META_KEY_FACILITATOR_STATUS,
    META_KEY_LANGUAGES,
    META_KEY_ORIGIN,
    META_KEY_PARTY_KIND,
    META_KEY_PRIMARY_EMAIL,
    map_contact,
    pivot_postmeta,
)
from jp_adopt_etl.mappers.status import UnmappedStatusError


def _post(post_id: int = 1, **kwargs) -> dict:
    base = {
        "ID": post_id,
        "post_title": "Test Adopter",
        "post_status": "publish",
        "post_date": None,
        "post_date_gmt": None,
    }
    base.update(kwargs)
    return base


def _meta(**kwargs) -> list[dict]:
    """Build a wp_postmeta-shaped list from kwargs. ``kwargs`` keys are
    meta_key values; values are meta_value."""
    return [
        {"meta_key": key, "meta_value": value}
        for key, value in kwargs.items()
    ]


# ─── pivot ────────────────────────────────────────────────────────────────


def test_pivot_postmeta_collapses_keys() -> None:
    rows = _meta(name="Alice", contact_email="alice@example.com")
    assert pivot_postmeta(rows) == {
        "name": "Alice",
        "contact_email": "alice@example.com",
    }


def test_pivot_postmeta_last_value_wins_on_duplicate_keys() -> None:
    rows = [
        {"meta_key": "name", "meta_value": "Alice"},
        {"meta_key": "name", "meta_value": "Bob"},
    ]
    assert pivot_postmeta(rows) == {"name": "Bob"}


def test_pivot_postmeta_skips_empty_key_rows() -> None:
    rows = [
        {"meta_key": None, "meta_value": "ignored"},
        {"meta_key": "", "meta_value": "ignored"},
        {"meta_key": "name", "meta_value": "Alice"},
    ]
    assert pivot_postmeta(rows) == {"name": "Alice"}


# ─── happy paths ──────────────────────────────────────────────────────────


def test_map_contact_minimal_adopter() -> None:
    post = _post(post_title="Alice")
    meta = _meta(
        **{
            META_KEY_PARTY_KIND: "adopter",
            META_KEY_PRIMARY_EMAIL: "Alice@Example.com",
            META_KEY_DISPLAY_NAME: "Alice Smith",
            META_KEY_ADOPTER_STATUS: "new",
        }
    )
    kwargs = map_contact(post_row=post, meta_rows=meta, mode="production")
    assert kwargs["party_kind"] == "adopter"
    assert kwargs["display_name"] == "Alice Smith"
    assert kwargs["adopter_status"] == "new"
    assert kwargs["facilitator_status"] is None
    assert kwargs["email_normalized"] == "alice@example.com"
    assert kwargs["source_system"] == "dt"
    assert kwargs["source_id"] == "1"
    assert kwargs["local_modified_after_import"] is False


def test_map_contact_facilitator_uses_facilitator_status() -> None:
    post = _post(post_id=42)
    meta = _meta(
        **{
            META_KEY_PARTY_KIND: "facilitator",
            META_KEY_PRIMARY_EMAIL: "fac@example.com",
            META_KEY_FACILITATOR_STATUS: "matched",  # → ready
            META_KEY_ADOPTER_STATUS: "engaged",  # ignored for facilitator
        }
    )
    kwargs = map_contact(post_row=post, meta_rows=meta, mode="production")
    assert kwargs["party_kind"] == "facilitator"
    assert kwargs["adopter_status"] is None
    assert kwargs["facilitator_status"] == "ready"


def test_map_contact_falls_back_to_post_title_when_meta_name_missing() -> None:
    post = _post(post_title="Post Title Fallback")
    meta = _meta(**{META_KEY_PARTY_KIND: "adopter"})
    kwargs = map_contact(post_row=post, meta_rows=meta, mode="production")
    assert kwargs["display_name"] == "Post Title Fallback"


def test_map_contact_uses_synthetic_name_when_everything_blank() -> None:
    post = _post(post_id=7, post_title="")
    kwargs = map_contact(post_row=post, meta_rows=[], mode="production")
    assert kwargs["display_name"] == "DT contact 7"


def test_map_contact_uppercases_country_code() -> None:
    post = _post()
    meta = _meta(**{META_KEY_COUNTRY_CODE: " us "})
    kwargs = map_contact(post_row=post, meta_rows=meta, mode="production")
    assert kwargs["country_code"] == "US"


def test_map_contact_normalizes_languages_csv() -> None:
    post = _post()
    meta = _meta(**{META_KEY_LANGUAGES: "EN, fr,  Es "})
    kwargs = map_contact(post_row=post, meta_rows=meta, mode="production")
    assert kwargs["language_codes"] == ["en", "fr", "es"]


def test_map_contact_normalizes_languages_php_serialized() -> None:
    post = _post()
    serialized = phpserialize.dumps(["en", "fr", "EN"]).decode("utf-8")
    meta = _meta(**{META_KEY_LANGUAGES: serialized})
    kwargs = map_contact(post_row=post, meta_rows=meta, mode="production")
    assert sorted(kwargs["language_codes"]) == ["en", "en", "fr"]


def test_map_contact_normalizes_origin_lowercase() -> None:
    post = _post()
    meta = _meta(**{META_KEY_ORIGIN: "Website"})
    kwargs = map_contact(post_row=post, meta_rows=meta, mode="production")
    assert kwargs["origin"] == "website"


# ─── error paths ──────────────────────────────────────────────────────────


def test_map_contact_dry_run_raises_on_unmapped_status() -> None:
    post = _post()
    meta = _meta(
        **{
            META_KEY_PARTY_KIND: "adopter",
            META_KEY_ADOPTER_STATUS: "weird_value",
        }
    )
    with pytest.raises(UnmappedStatusError):
        map_contact(post_row=post, meta_rows=meta, mode="dry_run")


def test_map_contact_production_maps_unmapped_status_to_unknown() -> None:
    post = _post()
    meta = _meta(
        **{
            META_KEY_PARTY_KIND: "adopter",
            META_KEY_ADOPTER_STATUS: "weird_value",
        }
    )
    kwargs = map_contact(post_row=post, meta_rows=meta, mode="production")
    assert kwargs["adopter_status"] == "unknown"


def test_map_contact_default_party_kind_is_adopter() -> None:
    """DT plugins can introduce other party kinds; we default to adopter
    and the orchestrator records a conflict (verified separately)."""
    post = _post()
    meta = _meta(**{META_KEY_PARTY_KIND: "missionary"})
    kwargs = map_contact(post_row=post, meta_rows=meta, mode="production")
    assert kwargs["party_kind"] == "adopter"
