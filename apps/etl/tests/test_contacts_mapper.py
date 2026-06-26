"""U9 contacts mapper unit tests.

Covers wp_postmeta pivot, phpserialize behavior, status mapping
integration, and edge cases (missing title, empty meta, weird types).
"""

from __future__ import annotations

import phpserialize
import pytest

from jp_adopt_etl.mappers.contacts import (
    META_KEY_ADOPTER_STATUS,
    META_KEY_DISPLAY_NAME,
    META_KEY_FACILITATOR_STATUS,
    META_KEY_OVERALL_STATUS,
    META_KEY_PARTY_KIND,
    META_KEY_SOURCES,
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


def test_party_kind_from_sub_type() -> None:
    post = _post(post_title="Alice")
    meta = _meta(
        **{
            META_KEY_PARTY_KIND: "adopter",
            META_KEY_DISPLAY_NAME: "Alice Smith",
            META_KEY_ADOPTER_STATUS: "new",
        }
    )
    kwargs = map_contact(post_row=post, meta_rows=meta, mode="production")
    assert kwargs["party_kind"] == "adopter"
    assert kwargs["display_name"] == "Alice Smith"
    assert kwargs["adopter_status"] == "new"
    assert kwargs["facilitator_status"] is None
    assert kwargs["source_system"] == "dt"
    assert kwargs["source_id"] == "1"
    assert kwargs["local_modified_after_import"] is False


def test_party_kind_ignores_dt_type_field() -> None:
    """DT ``type`` is access/user, NOT adopter/facilitator — only
    ``sub_type`` carries the party kind."""
    post = _post()
    meta = _meta(**{"type": "access", META_KEY_PARTY_KIND: "adopter"})
    kwargs = map_contact(post_row=post, meta_rows=meta, mode="production")
    assert kwargs["party_kind"] == "adopter"


def test_adopter_status_from_adopter_status_field_not_overall() -> None:
    """The JP ``adopter_status`` field is the lifecycle source; a generic
    ``overall_status='active'`` does NOT override it (only the terminal
    closed/unassignable signals do). A 'not_ready' adopter → potential_adopter."""
    post = _post()
    meta = _meta(
        **{
            META_KEY_PARTY_KIND: "adopter",
            META_KEY_ADOPTER_STATUS: "not_ready",
            META_KEY_OVERALL_STATUS: "active",
        }
    )
    kwargs = map_contact(post_row=post, meta_rows=meta, mode="production")
    assert kwargs["adopter_status"] == "potential_adopter"


def test_adopter_draft_from_adopter_status_field() -> None:
    """A saved-not-submitted form sign-up lands as adopter_status='draft' in
    DT (overall_status is 'active', a published post). Must surface as draft."""
    post = _post(post_status="publish")
    meta = _meta(
        **{
            META_KEY_PARTY_KIND: "adopter",
            META_KEY_ADOPTER_STATUS: "draft",
            META_KEY_OVERALL_STATUS: "active",
        }
    )
    kwargs = map_contact(post_row=post, meta_rows=meta, mode="production")
    assert kwargs["adopter_status"] == "draft"


def test_facilitator_status_from_facilitator_status_field() -> None:
    post = _post(post_id=42)
    meta = _meta(
        **{
            META_KEY_PARTY_KIND: "facilitator",
            META_KEY_FACILITATOR_STATUS: "ready",
            META_KEY_OVERALL_STATUS: "active",
        }
    )
    kwargs = map_contact(post_row=post, meta_rows=meta, mode="production")
    assert kwargs["party_kind"] == "facilitator"
    assert kwargs["adopter_status"] is None
    assert kwargs["facilitator_status"] == "ready"


def test_facilitator_draft_from_facilitator_status_field() -> None:
    post = _post(post_id=43)
    meta = _meta(
        **{
            META_KEY_PARTY_KIND: "facilitator",
            META_KEY_FACILITATOR_STATUS: "draft",
            META_KEY_OVERALL_STATUS: "active",
        }
    )
    kwargs = map_contact(post_row=post, meta_rows=meta, mode="production")
    assert kwargs["facilitator_status"] == "draft"


def test_overall_closed_overrides_to_do_not_engage() -> None:
    """A closed DT record is terminal regardless of pipeline stage."""
    post = _post()
    meta = _meta(
        **{
            META_KEY_PARTY_KIND: "adopter",
            META_KEY_ADOPTER_STATUS: "new",
            META_KEY_OVERALL_STATUS: "closed",
        }
    )
    kwargs = map_contact(post_row=post, meta_rows=meta, mode="production")
    assert kwargs["adopter_status"] == "do_not_engage"


def test_overall_unassignable_overrides_adopter_to_potential() -> None:
    """unassignable = the adopter still needs an FPG → potential_adopter
    (NOT opted-out), even when the pipeline field says 'new'."""
    post = _post()
    meta = _meta(
        **{
            META_KEY_PARTY_KIND: "adopter",
            META_KEY_ADOPTER_STATUS: "new",
            META_KEY_OVERALL_STATUS: "unassignable",
        }
    )
    kwargs = map_contact(post_row=post, meta_rows=meta, mode="production")
    assert kwargs["adopter_status"] == "potential_adopter"


def test_map_contact_falls_back_to_post_title_when_meta_name_missing() -> None:
    post = _post(post_title="Post Title Fallback")
    meta = _meta(**{META_KEY_PARTY_KIND: "adopter"})
    kwargs = map_contact(post_row=post, meta_rows=meta, mode="production")
    assert kwargs["display_name"] == "Post Title Fallback"


def test_map_contact_uses_synthetic_name_when_everything_blank() -> None:
    post = _post(post_id=7, post_title="")
    kwargs = map_contact(post_row=post, meta_rows=[], mode="production")
    assert kwargs["display_name"] == "DT contact 7"


def test_origin_from_sources_php_array_first_entry() -> None:
    post = _post()
    serialized = phpserialize.dumps(["Website", "referral"]).decode("utf-8")
    meta = _meta(**{META_KEY_SOURCES: serialized})
    kwargs = map_contact(post_row=post, meta_rows=meta, mode="production")
    assert kwargs["origin"] == "website"


def test_origin_from_sources_plain_string() -> None:
    post = _post()
    meta = _meta(**{META_KEY_SOURCES: "Website"})
    kwargs = map_contact(post_row=post, meta_rows=meta, mode="production")
    assert kwargs["origin"] == "website"


def test_email_and_phone_from_comm_channels() -> None:
    post = _post()
    meta = _meta(
        **{
            META_KEY_PARTY_KIND: "adopter",
            "contact_email_047": "Alice@Example.com",
            "contact_phone_285": "+1 555 0100",
        }
    )
    kwargs = map_contact(post_row=post, meta_rows=meta, mode="production")
    assert kwargs["email_normalized"] == "alice@example.com"
    assert kwargs["phone"] == "+1 555 0100"


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
