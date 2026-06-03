"""DT status enum → jp-adopt-core status enum mapping.

The mapping is documented in the build plan and reuses the table that the
sibling jp-adopt-platform launch-readiness migration validated against
real DT data. Two mappers — one per party kind — because DT's source
enum values overlap (`new`, `contacted`) but resolve to different target
states depending on whether the contact is an adopter or a facilitator.

Pure functions. No I/O. Tests live in ``tests/test_status_mapper.py``.
"""

from __future__ import annotations

from typing import Final, Literal

Mode = Literal["dry_run", "production"]


# ──────────────────────────────────────────────────────────────────────────
# Lookup tables
# ──────────────────────────────────────────────────────────────────────────


# Adopter side. The DT installation in production has used a handful of
# extra values over its lifetime (e.g. ``new_inquiry`` from the spike era,
# undocumented values from manual edits). Keep the table conservative and
# add new entries explicitly rather than guessing.
ADOPTER_STATUS_MAP: Final[dict[str, str]] = {
    "draft": "draft",
    "new": "new",
    "new_inquiry": "new",  # spike-era artifact; treated as new
    "contacted": "new",  # DT's "contacted" was lighter touch than ours
    "engaged": "contacted",
    "matched": "matched",
    "active": "matched",  # DT collapsed active into matched
    "inactive": "do_not_engage",
    # overall_status is the authoritative DT lifecycle source (see
    # .dt-inspection/decisions.md). 'unassignable' = closed / not pursued.
    "unassignable": "do_not_engage",
}


# Facilitator side. Same DT enum strings, different target semantics —
# the launch-readiness migration explicitly split the status concepts.
FACILITATOR_STATUS_MAP: Final[dict[str, str]] = {
    "draft": "draft",
    "new": "new",
    "contacted": "new",
    "engaged": "not_ready",
    "matched": "ready",
    "active": "ready",
    "inactive": "do_not_engage",
    "unassignable": "do_not_engage",
}


# When dry_run hits an unmapped value we fail loudly so the operator
# catches it before cutover. In production we still want the import to
# proceed: the row maps to ``unknown`` and a migration_conflicts row is
# written for Amy to reconcile manually. ``unknown`` is NOT a member of
# the CHECK constraint on contacts.adopter_status / facilitator_status —
# callers must store it in migration_conflicts.conflict_type or as a
# sentinel they understand; the mapper returns the literal so the
# orchestrator can route accordingly.
UNKNOWN_SENTINEL: Final[str] = "unknown"


# ──────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────


class UnmappedStatusError(ValueError):
    """Raised in dry_run mode when a DT status value has no mapping entry.

    Dry-run callers expect this to fail the entire run so the operator can
    add the new value to the lookup table before production cutover.
    """

    def __init__(self, party_kind: str, source_value: str) -> None:
        self.party_kind = party_kind
        self.source_value = source_value
        super().__init__(
            f"Unmapped {party_kind} status from DT: {source_value!r}. "
            f"Add an entry to status.py before re-running, or run with "
            f"--mode production to map it to {UNKNOWN_SENTINEL!r} and "
            f"continue with a migration_conflicts row."
        )


# ──────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────


def map_adopter_status(source_value: str | None, *, mode: Mode) -> str | None:
    """Map a DT adopter status string to the jp-adopt-core enum.

    Returns ``None`` when the source is ``None`` or empty (DT contacts can
    have NULL status). Returns ``UNKNOWN_SENTINEL`` for unmapped values
    in ``production`` mode; raises :class:`UnmappedStatusError` in
    ``dry_run`` mode.
    """
    if source_value is None:
        return None
    cleaned = source_value.strip().lower()
    if not cleaned:
        return None
    mapped = ADOPTER_STATUS_MAP.get(cleaned)
    if mapped is None:
        if mode == "dry_run":
            raise UnmappedStatusError("adopter", source_value)
        return UNKNOWN_SENTINEL
    return mapped


def map_facilitator_status(source_value: str | None, *, mode: Mode) -> str | None:
    """Map a DT facilitator status string to the jp-adopt-core enum.

    Same semantics as :func:`map_adopter_status`.
    """
    if source_value is None:
        return None
    cleaned = source_value.strip().lower()
    if not cleaned:
        return None
    mapped = FACILITATOR_STATUS_MAP.get(cleaned)
    if mapped is None:
        if mode == "dry_run":
            raise UnmappedStatusError("facilitator", source_value)
        return UNKNOWN_SENTINEL
    return mapped


__all__ = [
    "ADOPTER_STATUS_MAP",
    "FACILITATOR_STATUS_MAP",
    "Mode",
    "UNKNOWN_SENTINEL",
    "UnmappedStatusError",
    "map_adopter_status",
    "map_facilitator_status",
]
