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

import phpserialize
from jp_adopt_api.email_utils import normalize_email

from jp_adopt_etl.mappers.status import (
    Mode,
    map_adopter_status,
    map_facilitator_status,
)

logger = logging.getLogger(__name__)

# Meta keys the DT plugin uses for the fields we care about. Source:
# dt-adoption-fields/includes/custom-fields.php (the wishlist enumeration
# referenced in the build plan). When DT installs use a customized field
# slug, add it to the lookup; do not silently accept arbitrary key names.
META_KEY_PRIMARY_EMAIL = "contact_email"
META_KEY_DISPLAY_NAME = "name"
META_KEY_PARTY_KIND = "type"  # 'adopter' | 'facilitator'
META_KEY_ADOPTER_STATUS = "overall_status"  # DT's status column
META_KEY_FACILITATOR_STATUS = "facilitator_status"
META_KEY_COUNTRY_CODE = "country_code"
META_KEY_LANGUAGES = "languages"
META_KEY_ORIGIN = "origin"


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


def _maybe_deserialize_php(value: Any) -> Any:
    """If ``value`` is a string that looks like a serialized PHP array,
    deserialize. Otherwise return it unchanged.

    WordPress's ``maybe_serialize`` wraps arrays + objects but leaves
    scalars alone. The serialized shape always starts with ``a:`` (array),
    ``s:`` (string), ``i:`` (int), ``O:`` (object), or ``b:`` (bool) so we
    can detect cheaply before calling phpserialize.
    """
    if not isinstance(value, str):
        return value
    if not value or len(value) < 2 or value[1] != ":":
        return value
    try:
        return phpserialize.loads(value.encode("utf-8"), decode_strings=True)
    except (ValueError, TypeError, EOFError) as e:
        logger.warning("phpserialize.loads failed for %r: %s", value[:64], e)
        return value


def _normalize_languages(raw: Any) -> list[str] | None:
    """DT stores language codes as either a comma-separated string or a
    serialized PHP array depending on the field config. Coerce both to a
    list of lowercase 2-letter codes.
    """
    if raw is None:
        return None
    val = _maybe_deserialize_php(raw)
    if isinstance(val, dict):
        # phpserialize unpacks arrays into dicts keyed by int index
        candidates = list(val.values())
    elif isinstance(val, list):
        candidates = val
    elif isinstance(val, str):
        candidates = [c.strip() for c in val.split(",")]
    else:
        return None
    out = [str(c).strip().lower() for c in candidates if str(c).strip()]
    return out or None


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

    email = (meta.get(META_KEY_PRIMARY_EMAIL) or "").strip()
    display_name = (
        meta.get(META_KEY_DISPLAY_NAME)
        or post_row.get("post_title")
        or ""
    ).strip()
    if not display_name:
        # WordPress allows posts with empty titles; we always need
        # something so downstream UI doesn't render blank cells.
        display_name = f"DT contact {post_id}"

    adopter_status = (
        map_adopter_status(meta.get(META_KEY_ADOPTER_STATUS), mode=mode)
        if party_kind == "adopter"
        else None
    )
    facilitator_status = (
        map_facilitator_status(
            meta.get(META_KEY_FACILITATOR_STATUS), mode=mode
        )
        if party_kind == "facilitator"
        else None
    )

    country_code_raw = meta.get(META_KEY_COUNTRY_CODE)
    country_code = (
        str(country_code_raw).strip().upper()[:2] if country_code_raw else None
    )

    languages = _normalize_languages(meta.get(META_KEY_LANGUAGES))

    origin = (meta.get(META_KEY_ORIGIN) or "").strip().lower() or None

    # post_date_gmt is GMT-zoned per WP convention; coerce to a tz-aware
    # datetime for created_at. updated_at is left to the DB default — the
    # ETL is an import, not a state change.
    occurred_at = _coerce_datetime(post_row.get("post_date_gmt"))

    return {
        "party_kind": party_kind,
        "display_name": display_name,
        "adopter_status": adopter_status,
        "facilitator_status": facilitator_status,
        "email_normalized": normalize_email(email) if email else None,
        "country_code": country_code,
        "language_codes": languages,
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
    "META_KEY_ADOPTER_STATUS",
    "META_KEY_COUNTRY_CODE",
    "META_KEY_DISPLAY_NAME",
    "META_KEY_FACILITATOR_STATUS",
    "META_KEY_LANGUAGES",
    "META_KEY_ORIGIN",
    "META_KEY_PARTY_KIND",
    "META_KEY_PRIMARY_EMAIL",
    "map_contact",
    "pivot_postmeta",
]
