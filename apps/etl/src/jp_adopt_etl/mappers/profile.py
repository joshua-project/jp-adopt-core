"""DT ``wp_postmeta`` (post_type='contacts') → ``ContactProfile`` ORM kwargs.

The JP-custom adoption fields (≈30) map almost 1:1 from DT postmeta keys to
``contact_profile`` columns. This pure function pivots the typed values:
multi_select → list, booleans, ints, dates, and CHECK-constrained enums
(clamped to None when the DT value falls outside the column's allowed set so
a bad value can never raise a DB CHECK violation at insert time).

Returns ``None`` when the contact has no profile data at all (so the
orchestrator skips the upsert). See .dt-inspection/ASSESSMENT.md / decisions.md.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from jp_adopt_etl.mappers.php import loads_php_maybe

logger = logging.getLogger(__name__)

# CHECK domains — mirror jp_adopt_api.schemas Literal sets + migration 0016/0017.
_ENTITY_SIZE = {"1", "lt_30", "31_100", "101_500", "501_2000", "2001_plus"}
_ADOPTER_TYPE = {"individual", "small_group", "church", "organization", "network"}
_MOU_STATUS = {"signed", "not_required", "not_sent"}
_PREFERRED_COMMUNICATION = {"email", "phone"}

_STRING_FIELDS = (
    "primary_contact_name",
    "secondary_contact_name",
    "secondary_contact_email",
    "secondary_contact_phone",
    "website",
    "form_country",
    "form_state_region",
    "mou_signature_name",
    "doctrinal_distinctives",
    "accountability_memberships",
    "referral_source",
    "campaign",
    "partner",
    "additional_notes",
    "file_download_url",
)
_MULTISELECT_FIELDS = (
    "ministry_areas",
    "commitment_types",
    "facilitation_entity_types",
    "facilitation_entity_sizes",
    "facilitator_entity_types",
    "desired_facilitator_info",
    "network_partner_info",
)
_BOOL_FIELDS = (
    "works_with_fpgs",
    "willing_to_facilitate",
    "want_facilitator_connection",
    "want_network_connection",
    "has_doctrinal_distinctives",
    "has_accountability_membership",
)
_DATE_FIELDS = ("commitment_date", "last_contact_date", "next_followup_date")
_ENUM_FIELDS = {
    "entity_size": _ENTITY_SIZE,
    "adopter_type": _ADOPTER_TYPE,
    "mou_status": _MOU_STATUS,
    "preferred_communication": _PREFERRED_COMMUNICATION,
}


def _to_list(raw: Any) -> list[str] | None:
    """Coerce a DT multi_select (php-serialized array or CSV string) to a
    list of non-empty strings."""
    if raw is None or raw == "":
        return None
    val = loads_php_maybe(raw)
    if isinstance(val, dict):
        candidates = list(val.values())
    elif isinstance(val, list):
        candidates = val
    elif isinstance(val, str):
        candidates = [c.strip() for c in val.split(",")]
    else:
        return None
    out = [str(c).strip() for c in candidates if str(c).strip()]
    return out or None


def _to_bool(raw: Any) -> bool | None:
    if raw is None or raw == "":
        return None
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _to_int(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def _to_date(raw: Any) -> date | None:
    if raw is None or raw == "":
        return None
    try:
        return date.fromisoformat(str(raw).strip()[:10])
    except ValueError:
        return None


def map_contact_profile(meta: dict[str, Any]) -> dict[str, Any] | None:
    """Translate a wp_postmeta pivot into ``ContactProfile`` kwargs (without
    ``contact_id``). Returns ``None`` when no profile field is present."""
    out: dict[str, Any] = {}

    for key in _STRING_FIELDS:
        val = meta.get(key)
        if val is not None and str(val).strip():
            out[key] = str(val).strip()

    for key in _MULTISELECT_FIELDS:
        val = _to_list(meta.get(key))
        if val is not None:
            out[key] = val

    for key in _BOOL_FIELDS:
        val = _to_bool(meta.get(key))
        if val is not None:
            out[key] = val

    for key in _DATE_FIELDS:
        val = _to_date(meta.get(key))
        if val is not None:
            out[key] = val

    score = _to_int(meta.get("engagement_score"))
    if score is not None:
        out["engagement_score"] = score

    for key, domain in _ENUM_FIELDS.items():
        raw = meta.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        cleaned = str(raw).strip()
        if cleaned in domain:
            out[key] = cleaned
        else:
            # Out-of-domain value: clamp to None so the insert can't trip the
            # CHECK. The orchestrator records a migration_conflicts row.
            out[key] = None
            logger.warning("contact_profile.%s out of domain: %r", key, cleaned)

    return out or None


__all__ = ["map_contact_profile"]
