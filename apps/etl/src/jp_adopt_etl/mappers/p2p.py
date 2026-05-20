"""DT ``wp_p2p`` rows (contact ↔ FPG / contact ↔ facilitating_org) →
``AdopterInterest`` ORM kwargs.

DT uses the Posts-to-Posts plugin (wp_p2p table) to model many-to-many
relations between contacts and people-group posts. We only care about
the ``contacts_to_peoplegroups`` p2p_type (the FPG relation that
populates AdopterInterest.rop3) and ``contacts_to_groups`` (assigned
facilitator org, which populates the Match table — handled by a separate
mapper if/when we wire it in).

This v1 mapper handles the FPG relation only. Facilitator-org
assignments are deferred to U13 cutover since they tie into the live
match queue and Amy will want to review them by hand.
"""

from __future__ import annotations

import uuid
from typing import Any

# DT's p2p_type discriminator value for the FPG relation. Real DT
# installations may use a variant slug; the orchestrator filter is
# parameterized to allow override.
P2P_TYPE_CONTACT_TO_FPG = "contacts_to_peoplegroups"


def map_p2p_interest(
    *,
    p2p_row: dict[str, Any],
    contact_id: uuid.UUID,
    rop3: str,
) -> dict[str, Any]:
    """Translate one wp_p2p row into AdopterInterest kwargs.

    ``contact_id`` is the new Postgres Contact UUID (resolved by the
    orchestrator).
    ``rop3`` is the people-group rop3 code resolved from the
    ``p2p_to`` post's wp_postmeta (e.g. ``rop3 = 'AAA01'``). The
    orchestrator owns that lookup; the mapper just packages the result.

    AdopterInterest does not currently carry source_system / source_id
    columns (it's a child of Contact and resolves through Contact's
    source key). The p2p_id is preserved in commitment_level/notes only
    when DT stores a commitment_level meta on the p2p row.
    """
    return {
        "contact_id": contact_id,
        "rop3": rop3,
        # DT does not consistently capture a commitment level on p2p;
        # leave as None unless the orchestrator hydrated it.
        "commitment_level": p2p_row.get("commitment_level"),
        "notes": p2p_row.get("notes"),
    }


__all__ = ["P2P_TYPE_CONTACT_TO_FPG", "map_p2p_interest"]
