"""Extract DT contact comm channels (email + phone) from a postmeta pivot.

DT's ``contact_email`` / ``contact_phone`` fields are ``comm_channel`` types:
each value lives under a hashed key (``contact_email_<hash>``) with a sibling
``<key>_details`` php-serialized blob carrying flags like ``verified``. A
contact may have several of each. We surface a single primary (verified first,
then first-seen) plus any extras. See .dt-inspection/ASSESSMENT.md.

Pure function — no I/O.
"""

from __future__ import annotations

import re
from typing import Any

from jp_adopt_etl.mappers.php import loads_php_maybe

# Matches a channel value key (NOT its ``_details`` sibling). The hash is the
# DT field instance id, e.g. ``contact_email_047``.
_CHANNEL_KEY = re.compile(r"^(contact_email|contact_phone)_[0-9a-f]+$")


def _is_verified(details: Any) -> bool:
    if not details:
        return False
    parsed = loads_php_maybe(details)
    return bool(isinstance(parsed, dict) and parsed.get("verified"))


def extract_comm_channels(meta: dict[str, Any]) -> dict[str, Any]:
    """Return ``{"email", "extra_emails", "phone"}`` from a postmeta pivot.

    Primary email/phone is the first *verified* value, else the first value
    in key order. ``extra_emails`` holds the remaining emails (extra phones
    are dropped — the schema models a single ``contacts.phone``).
    """
    emails: list[tuple[bool, str]] = []
    phones: list[tuple[bool, str]] = []
    for key in sorted(meta.keys()):
        m = _CHANNEL_KEY.match(key)
        if not m:
            continue
        value = meta.get(key)
        if not value or not str(value).strip():
            continue
        verified = _is_verified(meta.get(f"{key}_details"))
        bucket = emails if m.group(1) == "contact_email" else phones
        bucket.append((verified, str(value).strip()))

    # Stable sort: verified entries first, original (key) order preserved within.
    emails.sort(key=lambda t: not t[0])
    phones.sort(key=lambda t: not t[0])

    return {
        "email": emails[0][1] if emails else None,
        "extra_emails": [e for _, e in emails[1:]],
        "phone": phones[0][1] if phones else None,
    }


__all__ = ["extract_comm_channels"]
