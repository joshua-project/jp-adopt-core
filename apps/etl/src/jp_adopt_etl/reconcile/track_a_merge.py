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


def is_real_name(name: str | None, email: str | None) -> bool:
    """True when ``name`` is a genuine person/org name, not DT's email fallback.

    DT seeds a contact's ``post_title`` from the email (whole address or its
    local-part) when no real name is known. Those are NOT names. Returns
    False for an empty name, or a name that — ignoring case and surrounding
    whitespace — equals the email or its local-part. With no email to
    compare against, any non-empty name is treated as real.
    """
    if name is None:
        return False
    stripped = name.strip()
    if not stripped:
        return False
    if not email:
        return True
    candidate = stripped.casefold()
    addr = email.strip().casefold()
    if candidate == addr:
        return False
    local_part = addr.split("@", 1)[0]
    return candidate != local_part


def resolve_display_name(
    *, core_name: str | None, dt_name: str | None, email: str | None
) -> str | None:
    """Name-aware ``display_name`` merge — return the value to SET, or None.

    DT-authoritative by default, with one safety carve-out: never replace a
    real core name with DT's email-as-name fallback (the prod bug where core
    "John Auer" was overwritten with "crossroads1947@yahoo.com" because that
    was all DT stored for the name). Using ``is_real_name`` against the
    conflict email:

    * DT name is a REAL name -> DT wins (overwrite).
    * DT name is NOT real (email-as-name) but core IS real -> keep core
      (return None, no change).
    * neither is real -> DT wins (authoritative default).

    Returns None for "no change" when DT has no name to write or the value
    already equals core (mirrors ``merge_descriptive``'s no-op skip).
    """
    if dt_name in (None, ""):
        return None
    if not is_real_name(dt_name, email) and is_real_name(core_name, email):
        return None
    if core_name == dt_name:
        return None
    return dt_name


def pick_winner(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the recommended-keep DT contact from a multi-collision cluster.

    Each candidate: ``{"source_id", "name", "email", "filled", "created"}``.
    Ranking (best first):
      (a) a real name (``is_real_name``) beats an email-as-name — a record
          with MORE fields but only an email for a name still loses to a
          genuinely-named record (the ``suranjansim`` bug);
      (b) higher ``filled`` (non-empty meta count);
      (c) newer ``created`` (ISO string compare);
      (d) stable by ``source_id`` ascending.
    Pure, no I/O.
    """
    # Rank candidates best-first; index 0 is the winner. We sort by the
    # "worse-is-larger" dimensions and use reverse=True on filled/created via
    # a tuple that flips their direction: real-name first (False < True for
    # `not real`), then higher filled, then newer created, then lowest
    # source_id. Build by sorting twice so each key keeps its natural
    # direction (stable sort), cheapest to read.
    ranked = sorted(
        candidates,
        key=lambda c: (c.get("source_id") or ""),
    )
    ranked = sorted(
        ranked,
        key=lambda c: (
            not is_real_name(c.get("name"), c.get("email")),
            -(c.get("filled") or 0),
            _DescStr(c.get("created") or ""),
        ),
    )
    return ranked[0]


@dataclass(frozen=True)
class _DescStr:
    """Wrap a string so it sorts in DESCENDING order (newer ISO date first)."""

    value: str

    def __lt__(self, other: _DescStr) -> bool:
        return self.value > other.value


__all__ = [
    "ConsentDecision",
    "consent_most_restrictive",
    "interests_to_add",
    "is_real_name",
    "merge_descriptive",
    "pick_winner",
    "resolve_display_name",
]
