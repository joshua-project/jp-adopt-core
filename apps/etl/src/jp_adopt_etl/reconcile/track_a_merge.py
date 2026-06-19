"""Pure merge-decision rules for the Track A DT-authoritative merge.

No SQLAlchemy / I/O — these operate on plain values, dicts and sets so the
merge policy unit-tests in isolation. The DB-touching apply path lives in
``track_a_duplicate_email.py`` and consumes these.

The three policy carve-outs from the design
(``docs/superpowers/specs/2026-06-18-track-a-dt-authoritative-merge-design.md``)
that are expressible as pure rules:

* descriptive fields — DT wins where DT has a value (most-recent curated
  data); core is kept only where DT is empty,
* consent — most-restrictive wins (an opt-out in DT *or* core stays
  opted-out; DT may never weaken a core opt-out),
* interests — union (add DT's, keep core's).

The open-match carve-out is a DB predicate, handled in the apply module.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


def merge_descriptive(core: dict[str, Any], dt: dict[str, Any]) -> dict[str, Any]:
    """DT-authoritative field merge: return only the columns to UPDATE.

    DT wins where it has a non-empty value. Columns where DT is empty
    (``None`` / ``""``) are omitted so the core value is kept, and columns
    whose DT value already equals the core value are omitted (no-op write).
    """
    changes: dict[str, Any] = {}
    for col, dt_val in dt.items():
        if dt_val in (None, ""):
            continue
        if core.get(col) != dt_val:
            changes[col] = dt_val
    return changes


@dataclass
class ConsentDecision:
    effective_optouts: set[str] = field(default_factory=set)
    dt_consents_to_add: set[str] = field(default_factory=set)


def consent_most_restrictive(
    *, core_optouts: set[str], dt_optouts: set[str]
) -> ConsentDecision:
    """Most-restrictive wins: the union of opt-outs is the effective set.

    ``dt_consents_to_add`` is empty: the safety requirement is to never
    WEAKEN a core opt-out, and the DT ETL imports no consent acceptance
    rows today (the orchestrator has no Consent path), so there is nothing
    to additively import. If additive DT-consent import is ever introduced,
    expand here (test first) to add only types opted out in NEITHER system.
    """
    effective = set(core_optouts) | set(dt_optouts)
    return ConsentDecision(effective_optouts=effective, dt_consents_to_add=set())


def interests_to_add(
    *, core_keys: Iterable[str], dt_keys: Iterable[str]
) -> set[str]:
    """Union semantics: DT interest keys not already on the core contact."""
    return set(dt_keys) - set(core_keys)


__all__ = [
    "ConsentDecision",
    "consent_most_restrictive",
    "interests_to_add",
    "merge_descriptive",
]
