"""Pure mapper functions translating DT MySQL row shapes → Postgres ORM kwargs.

Each module's ``map_*`` function takes raw DT row dicts and returns kwargs
ready for the Postgres ORM constructor. No I/O; nothing here writes to a
database. The orchestrator owns transactions and writes.
"""

from jp_adopt_etl.mappers.status import (
    UnmappedStatusError,
    map_adopter_status,
    map_facilitator_status,
)

__all__ = [
    "UnmappedStatusError",
    "map_adopter_status",
    "map_facilitator_status",
]
