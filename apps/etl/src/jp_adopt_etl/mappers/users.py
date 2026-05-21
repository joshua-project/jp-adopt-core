"""DT ``wp_users`` row → ``StaffIdentityLink`` kwargs.

Pure transformation. The wp_users password hash (PHPass) is intentionally
discarded — staff users get magic-link reset on first login post-cutover.
"""

from __future__ import annotations

from typing import Any

from jp_adopt_api.email_utils import normalize_email

# Special sentinel author id used for activity_log rows whose wp_users row
# was deleted before the snapshot. Comments mapper resolves missing
# authors to this string.
LEGACY_UNKNOWN_AUTHOR_ID: str = "system:dt_legacy_unknown"


def map_user(row: dict[str, Any]) -> dict[str, Any]:
    """Translate a wp_users row dict into kwargs for the StaffIdentityLink
    ORM. Required fields in the source row: ``ID``, ``user_email``.
    Optional: ``display_name``, ``user_login``, ``user_status``.

    Status semantics:
      * 0 / NULL  → "active"
      * non-zero  → "inactive"
      (wp_users.user_status is a small set in practice; we collapse to
      two operational states.)
    """
    dt_user_id = str(row["ID"])
    email = (row.get("user_email") or "").strip()
    display_name = (
        row.get("display_name")
        or row.get("user_login")
        or row.get("user_nicename")
        or None
    )
    raw_status = row.get("user_status", 0)
    try:
        status_int = int(raw_status) if raw_status is not None else 0
    except (TypeError, ValueError):
        status_int = 0
    status = "active" if status_int == 0 else "inactive"
    return {
        "dt_user_id": dt_user_id,
        "email": email,
        "email_normalized": normalize_email(email) if email else "",
        "display_name": display_name,
        "status": status,
        "source_system": "dt",
        "b2c_subject_id": None,  # populated later via separate matching step
    }


__all__ = ["LEGACY_UNKNOWN_AUTHOR_ID", "map_user"]
