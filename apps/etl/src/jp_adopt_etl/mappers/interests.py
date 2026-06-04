"""DT ``fpg_submission_data`` postmeta (JSON) → ``AdopterInterest`` kwargs.

DT stores per-FPG selections as a JSON array under the ``fpg_submission_data``
postmeta key — the *same* shape jp-adopt-forms produces (see mappers/forms.py).
This is the authoritative interest source; DT's ``wp_p2p`` table is unused
(empty). See .dt-inspection/ASSESSMENT.md.

Pure function — no I/O.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if str(x).strip()]


def parse_fpg_submission_data(raw: Any) -> list[dict[str, Any]]:
    """Parse the JSON array into a list of ``AdopterInterest`` kwargs (without
    ``contact_id``). Returns ``[]`` for empty/invalid input. Elements without a
    ``peopleId3`` are skipped (they cannot resolve to an FPG)."""
    if not raw or not isinstance(raw, str):
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as e:
        logger.warning("fpg_submission_data not valid JSON: %s", e)
        return []
    if not isinstance(parsed, list):
        return []

    out: list[dict[str, Any]] = []
    for el in parsed:
        if not isinstance(el, dict):
            continue
        people_id3 = el.get("peopleId3")
        if people_id3 is None or str(people_id3).strip() == "":
            continue
        out.append(
            {
                "people_id3": str(people_id3),
                "engagement_status": el.get("engagementStatus"),
                "facilitation_services": _str_list(el.get("facilitationServices")),
                "network_services": _str_list(el.get("networkServices")),
                "commitment_types": _str_list(el.get("commitmentTypes")),
            }
        )
    return out


__all__ = ["parse_fpg_submission_data"]
