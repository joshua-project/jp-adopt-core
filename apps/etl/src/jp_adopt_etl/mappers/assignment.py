"""DT ``assigned_to`` postmeta → wp_user_id.

DT stores the assigned staff member as ``assigned_to = 'user-<wp_user_id>'``.
``contact_assignment`` is 1:1 (contact_id PK), so only this primary owner is
migrated; DT's ``wp_dt_share`` sub-assignments have no 1:N target and are out
of scope. The orchestrator resolves the wp_user_id to a B2C subject via
``staff_identity_link``. Pure function — no I/O.
"""

from __future__ import annotations

import re

_ASSIGNED = re.compile(r"^user-(\d+)$")


def parse_assigned_user_id(assigned_to: str | None) -> str | None:
    """Return the wp_user_id from a ``user-<id>`` value, or ``None`` when
    unassigned / unparseable / user 0."""
    if not assigned_to:
        return None
    m = _ASSIGNED.match(str(assigned_to).strip())
    if not m:
        return None
    user_id = m.group(1)
    return None if user_id == "0" else user_id


__all__ = ["parse_assigned_user_id"]
