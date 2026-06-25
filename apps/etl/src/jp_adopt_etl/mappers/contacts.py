"""DT ``wp_posts`` (post_type='contacts') + ``wp_postmeta`` pivot →
``Contact`` ORM kwargs.

DT stores contact attributes as EAV (entity-attribute-value) rows in
``wp_postmeta``. This mapper takes one wp_posts row + a list of its
wp_postmeta rows, pivots them into a single dict, and returns Contact
kwargs.

Pure function (no DB I/O). The orchestrator does the actual fan-out
query to load wp_postmeta for a batch of post_ids.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from jp_adopt_api.email_utils import normalize_email

from jp_adopt_etl.mappers.channels import extract_comm_channels
from jp_adopt_etl.mappers.php import loads_php_maybe
from jp_adopt_etl.mappers.status import (
    Mode,
    map_adopter_status,
    map_facilitator_status,
)

logger = logging.getLogger(__name__)

# Meta keys verified against the real DT instance (see
# .dt-inspection/ASSESSMENT.md) — NOT the build-plan wishlist, which used
# several keys (`type`, `country_code`, `origin`, `languages`,
# `contact_email`) that do not exist in the actual data.
META_KEY_PARTY_KIND = "sub_type"  # 'adopter' | 'facilitator' (DT `type` is access/user)
META_KEY_DISPLAY_NAME = "name"  # usually absent; falls back to wp_posts.post_title
# overall_status is the authoritative lifecycle status. The dedicated
# adopter_status/facilitator_status postmeta are vestigial (always 'new').
META_KEY_OVERALL_STATUS = "overall_status"
META_KEY_SOURCES = "sources"  # multi_select; first entry → origin


def pivot_postmeta(meta_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Group a list of wp_postmeta rows by ``meta_key`` → ``meta_value``.

    Repeated meta_keys are not common for the fields we care about; when
    they do appear (DT plugin migrations occasionally leave dupes), the
    last value in the iteration order wins. The fixer-style alternative
    — collect into a list — would force every consumer to handle both
    scalar and list shapes for the same key.
    """
    pivoted: dict[str, Any] = {}
    for row in meta_rows:
        key = row.get("meta_key")
        value = row.get("meta_value")
        if not key:
            continue
        pivoted[key] = value
    return pivoted


def _first_source(raw: Any) -> str | None:
    """DT ``sources`` is a multi_select (php-serialized array). Take the
    first non-empty entry, lowercased, as the single-valued ``origin``.
    """
    if raw is None:
        return None
    val = loads_php_maybe(raw)
    if isinstance(val, dict):
        # phpserialize unpacks arrays into dicts keyed by int index
        candidates = list(val.values())
    elif isinstance(val, list):
        candidates = val
    elif isinstance(val, str):
        candidates = [val]
    else:
        return None
    for c in candidates:
        s = str(c).strip().lower()
        if s:
            return s
    return None


def _coerce_datetime(raw: Any) -> datetime | None:
    """DT timestamps are stored as MySQL DATETIME (naive). Coerce to UTC
    at the boundary — the WordPress timezone is per-site but the wp_posts
    table convention is UTC for post_date_gmt.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=UTC)
        return raw.astimezone(UTC)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def map_contact(
    *,
    post_row: dict[str, Any],
    meta_rows: list[dict[str, Any]],
    mode: Mode,
) -> dict[str, Any]:
    """Translate one wp_posts row (plus its wp_postmeta pivot) into Contact
    ORM kwargs.

    Returns a dict suitable for ``Contact(**kwargs)``. The caller is
    responsible for the actual INSERT … ON CONFLICT DO UPDATE.
    """
    post_id = str(post_row["ID"])
    meta = pivot_postmeta(meta_rows)

    party_kind_raw = (meta.get(META_KEY_PARTY_KIND) or "").strip().lower()
    if party_kind_raw not in ("adopter", "facilitator"):
        # DT permits other contact types via plugin extensions. We import
        # them as "adopter" (the dominant case) and flag for review via
        # the orchestrator's migration_conflicts hook — callers decide.
        party_kind = "adopter"
    else:
        party_kind = party_kind_raw

    display_name = (
        meta.get(META_KEY_DISPLAY_NAME)
        or post_row.get("post_title")
        or ""
    ).strip()
    if not display_name:
        # WordPress allows posts with empty titles; we always need
        # something so downstream UI doesn't render blank cells.
        display_name = f"DT contact {post_id}"

    overall_status = meta.get(META_KEY_OVERALL_STATUS)
    adopter_status = (
        map_adopter_status(overall_status, mode=mode)
        if party_kind == "adopter"
        else None
    )
    facilitator_status = (
        map_facilitator_status(overall_status, mode=mode)
        if party_kind == "facilitator"
        else None
    )

    # DT marks an incomplete/unsubmitted contact with WordPress's native
    # post_status='draft' (the plugin hides overall_status from staff, so a
    # draft carries no lifecycle status of its own). Surface those as 'draft'
    # rather than letting them default to 'new'/NULL. Only override when the
    # mapped status hasn't already advanced past 'new', so a genuinely-further
    # contact that happens to be an unpublished post is not demoted.
    post_status = (post_row.get("post_status") or "").strip().lower()
    if post_status == "draft":
        if party_kind == "adopter" and adopter_status in (None, "new"):
            adopter_status = "draft"
        elif party_kind == "facilitator" and facilitator_status in (None, "new"):
            facilitator_status = "draft"

    origin = _first_source(meta.get(META_KEY_SOURCES))

    channels = extract_comm_channels(meta)
    email = channels["email"]

    # post_date_gmt is GMT-zoned per WP convention; coerce to a tz-aware
    # datetime for created_at. updated_at is left to the DB default — the
    # ETL is an import, not a state change.
    occurred_at = _coerce_datetime(post_row.get("post_date_gmt"))

    return {
        "party_kind": party_kind,
        "display_name": display_name,
        "adopter_status": adopter_status,
        "facilitator_status": facilitator_status,
        # email + phone are multi-value DT comm channels (contact_email_<hash>);
        # extract_comm_channels picks the primary (verified-first).
        "email_normalized": normalize_email(email) if email else None,
        "phone": channels["phone"],
        "origin": origin,
        "source_system": "dt",
        "source_id": post_id,
        # Imported rows start un-edited; staff edits flip this to True.
        "local_modified_after_import": False,
        # created_at is normally set by the DB default; pass through the
        # WordPress timestamp so the audit trail preserves real history.
        # SQLAlchemy will use this when present.
        **({"created_at": occurred_at} if occurred_at is not None else {}),
    }


__all__ = [
    "META_KEY_DISPLAY_NAME",
    "META_KEY_OVERALL_STATUS",
    "META_KEY_PARTY_KIND",
    "META_KEY_SOURCES",
    "map_contact",
    "pivot_postmeta",
]
