"""Outbox suppression for bulk imports (U4).

The transactional outbox pattern emits one Outbox row per domain mutation
under normal traffic. During bulk imports (U9 DT ETL: 50K+ rows in a single
ETL run) this would create a fan-out storm — every webhook subscriber would
receive 50K events for a single operational action that the operator already
knows about.

The ``outbox_suppressed()`` context manager swaps the per-event emit for a
buffer; on exit, a single summary event (``jp.adopt.v1.bulk_imported``)
captures the run's metadata and the suppressed event types.

**Concurrency model:** uses ``contextvars.ContextVar`` (NOT ``threading.local``)
so the active-or-not bit travels with the asyncio Task that opened the
context. FastAPI runs many requests on the same OS thread; a thread-local
would leak suppression state across unrelated requests.

Usage (router side)::

    from jp_adopt_api.outbox_suppression import outbox_suppressed, emit_outbox

    @router.post("/v1/etl/import")
    async def bulk_import(...):
        async with outbox_suppressed("dt_etl_run", session) as ctx:
            for row in rows:
                # call code that uses ``emit_outbox(...)`` instead of writing
                # an Outbox() row directly.
                await import_one(row, session)
        # On exit, ctx.summary has counts; one ``bulk_imported`` Outbox row
        # has been written by ``__aexit__``.

Usage (mutation side)::

    from jp_adopt_api.outbox_suppression import emit_outbox

    async def some_mutation(session, contact):
        # ... mutate state ...
        emit_outbox(
            session,
            event_type="jp.adopt.v1.contact.updated",
            payload={"contact_id": str(contact.id), ...},
        )

When called outside a ``outbox_suppressed`` block, ``emit_outbox`` writes a
normal Outbox row. Inside, it buffers the event metadata and skips the DB
insert. The summary event on exit lists event_type → count.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from jp_adopt_api.models import Outbox

logger = logging.getLogger(__name__)

EVENT_BULK_IMPORTED = "jp.adopt.v1.bulk_imported"


@dataclass
class SuppressionContext:
    """State for a single ``outbox_suppressed()`` invocation."""

    label: str
    started_at: datetime
    event_counts: Counter[str] = field(default_factory=Counter)
    # Free-form metadata the caller wants in the summary payload (e.g.
    # rows_processed, source_system, watermark).
    metadata: dict[str, Any] = field(default_factory=dict)

    def record(self, event_type: str) -> None:
        self.event_counts[event_type] += 1

    @property
    def total_suppressed(self) -> int:
        return sum(self.event_counts.values())


# Module-level context variable. ``None`` means "no suppression active";
# any value means suppression is active for the calling Task.
_active: ContextVar[SuppressionContext | None] = ContextVar(
    "_outbox_suppression_active", default=None
)


def is_suppressed() -> bool:
    """Return True iff the calling Task is currently inside an
    ``outbox_suppressed()`` block. Provided so callers can branch without
    importing ``ContextVar`` themselves."""
    return _active.get() is not None


def emit_outbox(
    session: AsyncSession,
    *,
    event_type: str,
    payload: dict[str, Any],
    event_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Emit an outbox event — unless suppression is active.

    Returns the new Outbox row's UUID when an event was written, or ``None``
    when suppressed (so the audit row can still record the absence honestly).
    Callers MUST tolerate the ``None`` case rather than assuming a row exists.
    """
    ctx = _active.get()
    if ctx is not None:
        ctx.record(event_type)
        return None

    row_id = event_id or uuid.uuid4()
    session.add(
        Outbox(
            id=row_id,
            event_type=event_type,
            payload_json=payload,
        )
    )
    return row_id


@asynccontextmanager
async def outbox_suppressed(
    label: str,
    session: AsyncSession,
    *,
    metadata: dict[str, Any] | None = None,
) -> AsyncIterator[SuppressionContext]:
    """Open a suppression scope.

    Any ``emit_outbox`` call inside the scope (in the same Task) buffers
    instead of writing. On normal exit, one summary ``jp.adopt.v1.bulk_imported``
    Outbox row is added to ``session`` (caller commits) carrying the event
    type counts and any metadata the caller mutated on the context object.

    If the block raises, the summary event is NOT emitted; the caller's
    transaction is presumably going to roll back. The ``contextvars`` reset
    still runs (``finally`` block), so the suppression scope is closed.
    """
    if _active.get() is not None:
        # Nesting is intentionally not supported: a nested run would either
        # silently double-count or shadow the outer scope's metadata. Loud
        # failure is the right move — bulk importers should not be invoked
        # re-entrantly.
        raise RuntimeError(
            "outbox_suppressed() does not support nested scopes; "
            f"already inside {_active.get().label!r}"
        )

    ctx = SuppressionContext(
        label=label,
        started_at=datetime.now(UTC),
        metadata=dict(metadata or {}),
    )
    token = _active.set(ctx)
    try:
        yield ctx
    finally:
        _active.reset(token)

    # On clean exit (no exception), emit the summary. Anything that raised
    # would have skipped this line by exiting via the `finally` path above
    # without continuing.
    finished_at = datetime.now(UTC)
    summary_payload = {
        "event": EVENT_BULK_IMPORTED,
        "schema_version": "jp.adopt.v1",
        "label": ctx.label,
        "started_at": ctx.started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": (finished_at - ctx.started_at).total_seconds(),
        "total_suppressed_events": ctx.total_suppressed,
        "event_counts": dict(ctx.event_counts),
        "metadata": ctx.metadata,
    }
    session.add(
        Outbox(
            id=uuid.uuid4(),
            event_type=EVENT_BULK_IMPORTED,
            payload_json=summary_payload,
        )
    )
    logger.info(
        "outbox_suppression: label=%s suppressed=%d events=%s",
        ctx.label,
        ctx.total_suppressed,
        dict(ctx.event_counts),
    )
