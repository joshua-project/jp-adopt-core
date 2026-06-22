"""Track A — DT-authoritative ``duplicate_email`` merge.

Backlog in prod (2026-06-18): 210 ``duplicate_email`` rows on
``table_name='contacts'``. Each is a person who exists as BOTH a
jp-adopt-forms-seeded core contact (live) and a legacy Disciple.Tools (DT)
contact. DT is the record of truth — Amy curates contacts there — so this
tool merges the authoritative DT data ONTO the existing core contact.

Why the conflicts exist
-----------------------
DT permits the same email on multiple contacts; the new system enforces a
partial unique index (``uq_contacts_email_normalized`` WHERE
email_normalized IS NOT NULL). During the main ETL, when a second DT
contact carried an email already owned by an existing local contact, the
importer kept the row but DROPPED the colliding email to NULL and recorded
a ``duplicate_email`` conflict (``orchestrator._flush_contact_batch``,
source_value={'email_normalized': '<addr>'}, local_value=null).

That conflict row is LOSSY — it carries neither the DT payload nor the
merge-target contact id. So to reconcile one row we:

  (a) find the existing core **merge target** Contact by
      ``email_normalized == source_value['email_normalized']`` (the contact
      that kept the email), and
  (b) RE-READ the full DT contact by ``source_id`` (= the conflict's
      ``source_id`` = wp_posts.ID) via the gated DT reader.

The merge (DT-authoritative, three carve-outs)
----------------------------------------------
For each CLEAN conflict, DT overwrites the core contact:

  * **descriptive fields + workflow status** — DT wins where DT has a
    value (``merge_descriptive``); core kept only where DT is empty.
  * **child tables** — ContactProfile DT-overwrite; AdopterInterest union;
    Consent most-restrictive (never weaken a core opt-out); ActivityLog
    append; ContactAssignment DT-replace (``_merge_children`` +
    ``_repoint_history_and_assignment``).
  * **identity_link** — record the colliding email as an auth identity
    pointing at the merge target's B2C subject so a future login resolves
    to the surviving contact.
  * **durable resolution** — the target adopts the DT keys
    (``source_system='dt'``, ``source_id``) so the next hourly sync
    resolves it by key (update path) and never re-collides; the DT loser
    stub is deleted and the MigrationConflict row removed.

Three carve-outs route to Amy instead of auto-merging:

  1. **Open core match** — a contact with a match in
     ``OPEN_MATCH_STATUSES`` is left untouched (protects live in-core
     triage from a stale DT status reset) and flagged in the review list.
  2. **Ambiguous identity** — same email but a DT/core name mismatch
     (likely a shared/family inbox) is not auto-merged; goes to review.
  3. **Consent most-restrictive** — an opt-out in DT OR core stays
     opted-out; DT may never weaken a core opt-out.

Safety
------
* DEFAULTS TO DRY-RUN. ``--apply`` (or ``mode='production'``) is required
  to write; dry-run rolls back every data write but still surfaces an
  ``etl_run`` audit row (mirrors ``orchestrator.run_etl``).
* Bulk writes go through ``outbox_suppressed`` — one
  ``jp.adopt.v1.bulk_imported`` summary event, never per-row Outbox.
* Idempotent: merged contacts become ``source_system='dt'`` and no longer
  surface as conflicts; their conflict rows are deleted; field overwrites
  are deterministic; child upserts use ON CONFLICT — so apply-twice is a
  no-op.
* DT MySQL is credential-gated and unavailable in CI — the DT reader is
  injectable (``dt_reader=``) so tests mock it; the real ``--apply`` is
  operator-led.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from jp_adopt_api.email_utils import normalize_email
from jp_adopt_api.models import (
    ActivityLog,
    AdopterInterest,
    Contact,
    ContactAssignment,
    ContactProfile,
    EtlRun,
    IdentityLink,
    Match,
    MigrationConflict,
)
from jp_adopt_api.outbox_suppression import outbox_suppressed
from sqlalchemy import create_engine, delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from jp_adopt_etl.dt_source import (
    fetch_contact,
    load_postmeta,
    open_engine,
)
from jp_adopt_etl.mappers.contacts import map_contact, pivot_postmeta
from jp_adopt_etl.mappers.interests import parse_fpg_submission_data
from jp_adopt_etl.mappers.profile import map_contact_profile
from jp_adopt_etl.mappers.status import Mode
from jp_adopt_etl.orchestrator import _load_existing_fpg_ids
from jp_adopt_etl.reconcile.track_a_merge import (
    interests_to_add,
    merge_descriptive,
    pick_winner,
    resolve_display_name,
)

# A core contact carrying any of these signals is PROTECTED — the
# DT-authoritative merge never overwrites it; it is skipped and flagged for
# Amy. These are the opt-out / most-restrictive signals: ``do_not_engage`` is
# an explicit human "do not contact" disposition, and
# ``local_modified_after_import`` means staff edited the contact in core after
# import. This mirrors the ETL importer's own guard
# (``orchestrator._flush_contact_batch``: the upsert's
# ``where=Contact.local_modified_after_import.is_(False)`` records a
# ``local_modified_after_import`` conflict instead of overwriting). See the
# design doc carve-out #3.
_DO_NOT_ENGAGE = "do_not_engage"

logger = logging.getLogger(__name__)

CONFLICT_TYPE = "duplicate_email"
TABLE_NAME = "contacts"
SOURCE_SYSTEM = "dt"
RECONCILE_LABEL = "dt_reconcile:duplicate_email"

# "Open" match statuses — a contact with a match in any of these is in
# active in-core triage and must NOT have its workflow status reset by the
# DT-authoritative merge. A Match has no direct contact FK — it hangs off
# adopter_interest — so the predicate joins Match → AdopterInterest by
# contact_id.
#
# This is intentionally a SUPERSET of the DB ``uq_match_open_per_interest``
# partial unique index (which covers recommended / accepted / active /
# triage). ``sent_back`` is added here because a sent-back match is
# mid-triage — a human bounced it back for rework and is actively deciding —
# so the contact is still "live" for the purpose of NOT clobbering its core
# status from DT. The DB index legitimately excludes ``sent_back`` (a
# sent-back match is not "open" for the one-open-match-per-interest
# invariant), but this is a read-only skip predicate with a more
# conservative goal: when in doubt, leave a contact under any active human
# attention for Amy rather than auto-overwrite. The remaining terminal
# statuses (declined / completed / withdrawn) are genuinely done and not
# protected here.
OPEN_MATCH_STATUSES = ("recommended", "accepted", "active", "triage", "sent_back")

# Contact columns the DT-authoritative merge overwrites onto the target
# where DT has a value (``merge_descriptive`` keeps the core value only
# where DT is empty). adopter_status / facilitator_status are INCLUDED:
# the merge is DT-authoritative and two pre-checks have already excluded
# anything we must not touch — the open-match skip (live in-core triage) and
# the protected-contact skip (do_not_engage / local_modified_after_import,
# ``plan_merges``). Only after both filters do we write status directly.
#
# This is NOT a blanket "the ETL importer also writes status directly" — the
# importer is GUARDED: its upsert carries
# ``where=Contact.local_modified_after_import.is_(False)`` and routes
# locally-edited rows to a conflict instead of overwriting. We mirror that
# guard with the ``skip_protected`` carve-out below, so a direct status
# write here only ever lands on an unprotected, non-live contact.
#
# email_normalized is excluded — the target already owns the email; the DT
# loser's email is folded in via the identity link + key adoption, not a
# column overwrite.
_MERGE_FIELDS = (
    "display_name",
    "phone",
    "origin",
    "country_code",
    "adopter_status",
    "facilitator_status",
)

# The subset of ``_MERGE_FIELDS`` that take the plain DT-wins-where-non-empty
# behavior (``merge_descriptive``). ``display_name`` is excluded: it is merged
# name-aware (``resolve_display_name``) so a real core name is never replaced
# by DT's email-as-name fallback. See ``plan_merges``.
_PLAIN_MERGE_FIELDS = tuple(f for f in _MERGE_FIELDS if f != "display_name")

# Status values that are NOT valid members of the contacts CHECK
# constraints (ck_contacts_adopter_status / _facilitator_status). The
# status mapper emits ``unknown`` for DT values it can't map; writing it
# would violate the constraint, so we drop those from the overwrite (the
# core status is kept and the row is surfaced elsewhere for review).
_INVALID_STATUS_VALUES = ("unknown",)
_STATUS_FIELDS = ("adopter_status", "facilitator_status")


# A DT reader callable: (conn, post_id) -> (post_row | None, meta_rows).
DtReader = Callable[[Any, str], "tuple[dict[str, Any] | None, list[dict[str, Any]]]"]


@dataclass
class Decisions:
    """Operator review decisions fed back into planning after a human looks
    at the ambiguous-name / shared-email cases.

    * ``force_merge`` — normalized emails the reviewer confirmed are the SAME
      person, so an ambiguous-name ``review`` plan is upgraded to ``merge``.
      NEVER punches through a safety carve-out (open match / protected /
      missing target).
    * ``multi_keep`` — for a shared-email (multi-collision) cluster, the ONE
      DT ``source_id`` to keep+merge (``{normalized_email: source_id}``). That
      member flows through normal merge planning; the rest stay
      ``skip_multi_collision``.
    """

    force_merge: set[str] = field(default_factory=set)
    multi_keep: dict[str, str] = field(default_factory=dict)


def load_decisions(path: str | None) -> Decisions:
    """Load an operator decisions JSON file into a :class:`Decisions`.

    Shape::

        {
          "force_merge": ["email1", ...],
          "multi_keep": {"email": "source_id", ...}
        }

    Pure helper. A missing path / file or missing keys yield empty defaults;
    every email key is normalized with ``normalize_email`` so it matches the
    normalized emails used in planning.
    """
    if not path:
        return Decisions()
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return Decisions()
    force_merge = {
        normalize_email(e) for e in (raw.get("force_merge") or []) if e
    }
    multi_keep = {
        normalize_email(email): source_id
        for email, source_id in (raw.get("multi_keep") or {}).items()
        if email
    }
    return Decisions(force_merge=force_merge, multi_keep=multi_keep)


@dataclass
class MergePlan:
    """One reconciliation decision for a single ``duplicate_email`` row."""

    source_id: str
    email_normalized: str
    target_contact_id: uuid.UUID | None = None
    target_display_name: str | None = None
    loser_contact_id: uuid.UUID | None = None
    dt_display_name: str | None = None
    # Fields DT overwrites onto the target {column: value}.
    field_changes: dict[str, Any] = field(default_factory=dict)
    # DT child data carried from planning to apply.
    dt_profile: dict[str, Any] | None = None
    dt_interests: list[dict[str, Any]] = field(default_factory=list)
    activity_repointed: int = 0
    interests_repointed: int = 0
    assignment_repointed: bool = False
    identity_linked: bool = False
    # For multi-collision clusters only: the source_id of the DT contact the
    # reviewer is RECOMMENDED to keep (most-complete, name-aware). Advisory —
    # multi-collision stays review-only; this does not auto-merge or delete.
    recommended_keep: str | None = None
    # Disposition: 'merge' | 'review' | 'skip_open_match' | 'skip_protected'
    # | 'skip_multi_collision' | 'skip_missing_target' | 'failed'.
    status: str = "pending"
    reason: str = ""


@dataclass
class ReconcileResult:
    planned: list[MergePlan] = field(default_factory=list)

    @property
    def to_merge(self) -> list[MergePlan]:
        return [p for p in self.planned if p.status == "merge"]

    @property
    def to_review(self) -> list[MergePlan]:
        return [p for p in self.planned if p.status == "review"]

    @property
    def skipped(self) -> list[MergePlan]:
        return [p for p in self.planned if p.status.startswith("skip")]

    @property
    def failed(self) -> list[MergePlan]:
        return [p for p in self.planned if p.status == "failed"]

    @property
    def for_review_list(self) -> list[MergePlan]:
        """Cases needing an Amy decision: ambiguous names, open-match skips,
        protected contacts (do_not_engage / locally edited), and
        multi-collision emails. (``skip_missing_target`` is data hygiene, not
        a review item.)"""
        return [
            p
            for p in self.planned
            if p.status
            in (
                "review",
                "skip_open_match",
                "skip_protected",
                "skip_multi_collision",
            )
        ]

    def counts(self) -> dict[str, int]:
        # NOTE: call AFTER apply — a per-contact SAVEPOINT failure flips a
        # plan's status from 'merge' to 'failed', so it drops out of
        # ``to_merge`` and is added to ``rows_out_skipped`` via the explicit
        # ``+ len(self.failed)`` term below (failed rows are not "inserted";
        # 'failed' does not match the ``skip`` prefix). In dry-run nothing
        # applies, so all 'merge' plans stay.
        return {
            "rows_in": len(self.planned),
            "rows_out_inserted": len(self.to_merge),
            "rows_out_skipped": len(self.skipped) + len(self.to_review)
            + len(self.failed),
            "rows_in_review": len(self.to_review),
        }


# ──────────────────────────────────────────────────────────────────────────
# DT re-read adapter
# ──────────────────────────────────────────────────────────────────────────


def _default_dt_reader(
    conn: Connection, source_id: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Re-read one DT contact (+ its postmeta) by source_id via the gated
    reader. Returns ``(post_row | None, meta_rows)``."""
    try:
        post_id: Any = int(source_id)
    except (TypeError, ValueError):
        post_id = source_id
    post_row = fetch_contact(conn, post_id)
    if post_row is None:
        return None, []
    meta = load_postmeta(conn, [post_row["ID"]])
    return post_row, meta.get(post_row["ID"], [])


def _count_filled(meta_rows: list[dict[str, Any]]) -> int:
    """Count meta rows whose value is non-empty — a rough "how complete is
    this DT contact" signal for the recommended-keeper heuristic. DT postmeta
    rows carry the value under ``meta_value`` (``dt_source.load_postmeta``);
    fall back to ``value`` for resilience to alternate reader shapes."""
    filled = 0
    for row in meta_rows:
        val = row.get("meta_value", row.get("value"))
        if val not in (None, ""):
            filled += 1
    return filled


def _recommend_keeper(
    mysql_conn: Any, email: str, source_ids: list[str], dt_reader: DtReader,
) -> str | None:
    """Re-read every DT contact in a multi-collision cluster and return the
    source_id of the recommended keeper (name-aware "most complete").

    The cluster's shared collision ``email`` is what each candidate's
    ``post_title`` is tested against — a DT post whose title IS the email is
    the fallback-named one and loses to a genuinely-named sibling.

    Re-read failures are tolerated: a candidate whose re-read returns None is
    scored with ``name=None`` / ``filled=0`` so it ranks last but
    ``pick_winner`` still produces a recommendation. Advisory only — the
    cluster stays review-only; nothing is merged or deleted here.
    """
    candidates: list[dict[str, Any]] = []
    for sid in source_ids:
        try:
            post_row, meta_rows = dt_reader(mysql_conn, sid)
        except Exception:  # noqa: BLE001 — a bad re-read must not abort scoring
            post_row, meta_rows = None, []
        if post_row is None:
            candidates.append(
                {"source_id": sid, "name": None, "email": email,
                 "filled": 0, "created": None}
            )
            continue
        candidates.append(
            {
                "source_id": sid,
                "name": post_row.get("post_title"),
                "email": email,
                "filled": _count_filled(meta_rows),
                "created": post_row.get("post_date"),
            }
        )
    if not candidates:
        return None
    return pick_winner(candidates)["source_id"]


# ──────────────────────────────────────────────────────────────────────────
# Name-similarity heuristic for ambiguity detection
# ──────────────────────────────────────────────────────────────────────────


def _norm_name(name: str | None) -> str:
    """Lowercase + collapse whitespace, drop punctuation, for fuzzy compare."""
    if not name:
        return ""
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in name)
    return " ".join(cleaned.lower().split())


def names_look_like_same_person(a: str | None, b: str | None) -> bool:
    """Conservative same-person check.

    True when the two display names plausibly refer to the same person:
    exact (normalized) match, or one's token set is a subset of the other's
    (e.g. 'Jane Doe' vs 'Jane M. Doe'), or they share a surname-ish token.
    A blank name on either side is treated as "no signal" => same-person
    (we don't want a missing DT title to manufacture review noise).

    False => AMBIGUOUS (likely shared/family inbox) => goes to review list.
    """
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return True
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    if ta <= tb or tb <= ta:
        return True
    # Share at least one token of length >= 3 (surname-ish overlap).
    shared = {t for t in (ta & tb) if len(t) >= 3}
    return bool(shared)


# ──────────────────────────────────────────────────────────────────────────
# Planning (pure-ish: reads PG + DT, decides, no writes)
# ──────────────────────────────────────────────────────────────────────────


def _load_conflicts(pg_session: Session) -> list[MigrationConflict]:
    return list(
        pg_session.execute(
            select(MigrationConflict).where(
                MigrationConflict.source_system == SOURCE_SYSTEM,
                MigrationConflict.table_name == TABLE_NAME,
                MigrationConflict.conflict_type == CONFLICT_TYPE,
            )
        )
        .scalars()
        .all()
    )


def plan_merges(
    *,
    pg_session: Session,
    mysql_conn: Any,
    dt_reader: DtReader = _default_dt_reader,
    mode: Mode = "dry_run",
    decisions: Decisions | None = None,
) -> ReconcileResult:
    """Decide what to do with every ``duplicate_email`` conflict.

    No writes. Each conflict becomes a :class:`MergePlan` whose ``status``
    is ``merge`` (clear, auto-mergeable), ``review`` (ambiguous name —
    needs Amy), ``skip_open_match`` / ``skip_protected`` /
    ``skip_multi_collision`` (routed to Amy), or ``skip_missing_target``
    (the merge target no longer carries the email; nothing to merge into).

    ``decisions`` (optional) feeds reviewer overrides back in: ``force_merge``
    upgrades an ambiguous-name ``review`` to ``merge`` (never punching through
    a safety carve-out), and ``multi_keep`` lets the chosen keeper of a
    shared-email cluster flow through normal merge planning while the rest stay
    ``skip_multi_collision``. With no decisions the behavior is unchanged.
    """
    decisions = decisions or Decisions()
    result = ReconcileResult()
    conflicts = _load_conflicts(pg_session)

    # Pre-scan for multi-collision emails: if the SAME email_normalized
    # appears in more than one duplicate_email conflict, every DT post on
    # that email collides onto one core contact. Durable key adoption
    # (target adopts ('dt', source_id)) can only represent ONE DT post ->
    # one contact, so a many-DT-posts -> one-contact case is not auto-
    # mergeable. Route ALL of them to Amy (skip_multi_collision) instead.
    email_counts: dict[str, int] = {}
    # email -> [source_id, ...] of every DT post colliding on it, so a
    # multi-collision cluster can be scored together for a recommended keeper.
    email_members: dict[str, list[str]] = {}
    for conflict in conflicts:
        raw = (conflict.source_value or {}).get("email_normalized")
        norm = normalize_email(raw) if raw else None
        if norm:
            email_counts[norm] = email_counts.get(norm, 0) + 1
            email_members.setdefault(norm, []).append(conflict.source_id)

    # For each multi-collision email, score every candidate DT contact once
    # (name-aware "most complete") and cache the recommended keeper's
    # source_id. Advisory only: multi-collision stays in the review list; we
    # never auto-merge or delete losers (a "most complete" pick can be a
    # different real entity — e.g. an org beats a person — so discarding
    # losers needs human confirmation).
    recommended_keep_by_email: dict[str, str | None] = {}
    for norm, members in email_members.items():
        if email_counts.get(norm, 0) <= 1:
            continue
        recommended_keep_by_email[norm] = _recommend_keeper(
            mysql_conn, norm, members, dt_reader
        )

    for conflict in conflicts:
        source_id = conflict.source_id
        raw_email = (conflict.source_value or {}).get("email_normalized")
        email = normalize_email(raw_email) if raw_email else None
        plan = MergePlan(source_id=source_id, email_normalized=email or "")
        if not email:
            plan.status = "skip_missing_target"
            plan.reason = "conflict row has no email_normalized"
            result.planned.append(plan)
            continue

        # Carve-out: multi-collision email — more than one DT post maps onto
        # this same email/contact. Not representable by single-key adoption;
        # route every member to Amy — UNLESS the operator picked a keeper.
        if email_counts.get(email, 0) > 1:
            chosen_keeper = decisions.multi_keep.get(email)
            # MULTI_KEEP override: the operator picked the ONE source_id to
            # keep+merge. Only honor it when the chosen keeper is actually a
            # member of this cluster (else leave the whole cluster skipped and
            # warn). The chosen member falls through to normal merge planning;
            # every OTHER member stays skip_multi_collision.
            keeper_in_cluster = (
                chosen_keeper is not None
                and chosen_keeper in email_members.get(email, [])
            )
            if chosen_keeper is not None and not keeper_in_cluster:
                logger.warning(
                    "track_a: multi_keep keeper %s not found in cluster for "
                    "email %s; leaving whole cluster skip_multi_collision",
                    chosen_keeper,
                    email,
                )
            if not (keeper_in_cluster and source_id == chosen_keeper):
                plan.status = "skip_multi_collision"
                plan.recommended_keep = recommended_keep_by_email.get(email)
                if keeper_in_cluster:
                    plan.reason = (
                        f"superseded by operator keeper {chosen_keeper}"
                    )
                else:
                    plan.reason = (
                        f"email {email!r} collides from {email_counts[email]} "
                        "DT posts; many-to-one not auto-mergeable, left for Amy"
                    )
                    if plan.recommended_keep:
                        plan.reason += (
                            f" (recommended keep: {plan.recommended_keep})"
                        )
                result.planned.append(plan)
                continue
            # else: this conflict IS the operator-chosen keeper — fall through
            # to the normal merge-planning path below (re-read DT, compute
            # field_changes/children, run the open-match/protected checks).

        # (a) merge target — the surviving contact that kept this email.
        target = pg_session.execute(
            select(Contact).where(Contact.email_normalized == email)
        ).scalars().first()
        if target is None:
            plan.status = "skip_missing_target"
            plan.reason = f"no local contact owns email {email!r}"
            result.planned.append(plan)
            continue
        plan.target_contact_id = target.id
        plan.target_display_name = target.display_name

        # Carve-out: protected contact — never overwrite a do_not_engage or
        # staff-edited (local_modified_after_import) contact. Mirrors the ETL
        # importer's ``where=local_modified_after_import.is_(False)`` guard
        # (and extends it to do_not_engage). Skip + flag for Amy. See
        # ``_DO_NOT_ENGAGE`` / the design carve-out #3.
        if (
            target.adopter_status == _DO_NOT_ENGAGE
            or target.facilitator_status == _DO_NOT_ENGAGE
            or target.local_modified_after_import
        ):
            plan.status = "skip_protected"
            plan.reason = (
                "core contact is protected (do_not_engage or "
                "local_modified_after_import); never overwritten, left for Amy"
            )
            result.planned.append(plan)
            continue

        # Carve-out 1: open core match => skip + flag for Amy. A stale DT
        # status reset must never clobber a contact under active in-core
        # triage. Match has no contact FK — it references adopter_interest —
        # so join through it.
        open_match = pg_session.execute(
            select(Match.id)
            .join(
                AdopterInterest,
                Match.adopter_interest_id == AdopterInterest.id,
            )
            .where(
                AdopterInterest.contact_id == target.id,
                Match.status.in_(OPEN_MATCH_STATUSES),
            )
        ).first()
        if open_match is not None:
            plan.status = "skip_open_match"
            plan.reason = "contact has an open match in core; left for Amy"
            result.planned.append(plan)
            continue

        # The DT loser contact (its email was dropped to NULL on import).
        loser = pg_session.execute(
            select(Contact).where(
                Contact.source_system == SOURCE_SYSTEM,
                Contact.source_id == source_id,
            )
        ).scalars().first()
        plan.loser_contact_id = loser.id if loser is not None else None

        # (b) re-read the full DT contact by source_id.
        post_row, meta_rows = dt_reader(mysql_conn, source_id)
        if post_row is None:
            plan.status = "skip_missing_target"
            plan.reason = f"DT contact {source_id} not found on re-read"
            result.planned.append(plan)
            continue

        # ALWAYS map in 'production' mode regardless of the tool's mode: in
        # 'dry_run' the status mappers RAISE UnmappedStatusError on any
        # unmapped status, which would abort the whole diagnostics run on a
        # single bad row AND make dry-run diverge from --apply. 'production'
        # maps unmapped statuses to the 'unknown' sentinel instead, so the
        # rehearsal faithfully mirrors what --apply would do and never crashes.
        dt_kwargs = map_contact(
            post_row=post_row, meta_rows=meta_rows, mode="production"
        )
        plan.dt_display_name = dt_kwargs.get("display_name")

        # DT-authoritative field plan: DT wins where DT has a value; the core
        # value is kept where DT is empty. Status columns whose DT value is
        # unmappable (``unknown``) are dropped so the overwrite never
        # violates the contacts status CHECK constraints. ``display_name`` is
        # NOT a plain DT-wins column — it is merged name-aware below so a real
        # core name is never overwritten with DT's email-as-name fallback.
        core_vals = {col: getattr(target, col, None) for col in _PLAIN_MERGE_FIELDS}
        dt_vals = {col: dt_kwargs.get(col) for col in _PLAIN_MERGE_FIELDS}
        for col in _STATUS_FIELDS:
            if dt_vals.get(col) in _INVALID_STATUS_VALUES:
                dt_vals[col] = None
        plan.field_changes = merge_descriptive(core=core_vals, dt=dt_vals)

        # display_name: name-aware. DT wins UNLESS DT only stored the email as
        # the name and core holds a real name (then keep core). The conflict
        # email is the same one used elsewhere in planning.
        resolved_name = resolve_display_name(
            core_name=target.display_name,
            dt_name=dt_kwargs.get("display_name"),
            email=email,
        )
        if resolved_name is not None:
            plan.field_changes["display_name"] = resolved_name

        # Child-table inputs from the same DT postmeta pivot. Carried on the
        # plan so the apply step merges them without re-reading DT.
        dt_meta = pivot_postmeta(meta_rows)
        plan.dt_profile = map_contact_profile(dt_meta)
        plan.dt_interests = parse_fpg_submission_data(
            dt_meta.get("fpg_submission_data")
        )

        # Ambiguity gate: same email but mismatched name => possible shared
        # inbox. Route to the review list instead of auto-merging — UNLESS the
        # operator confirmed (force_merge) it is the same person. The
        # dt_kwargs/field_changes/children were already computed above, so a
        # force_merge override just flips status to 'merge'. This NEVER fires
        # for a skip_open_match / skip_protected / skip_missing_target contact:
        # those carve-outs already `continue`-d before reaching here, so the
        # decision can only upgrade an ambiguous-name review, never punch
        # through a safety skip.
        if not names_look_like_same_person(
            plan.dt_display_name, plan.target_display_name
        ):
            if email in decisions.force_merge:
                plan.status = "merge"
                plan.reason = "operator override: confirmed same person"
                result.planned.append(plan)
                continue
            plan.status = "review"
            plan.reason = (
                "email shared but names differ: DT "
                f"{plan.dt_display_name!r} vs local "
                f"{plan.target_display_name!r}"
            )
            result.planned.append(plan)
            continue

        plan.status = "merge"
        plan.reason = "email + name consistent; auto-mergeable"
        result.planned.append(plan)
    return result


# ──────────────────────────────────────────────────────────────────────────
# Apply one merge (writes; called only inside the suppressed transaction)
# ──────────────────────────────────────────────────────────────────────────


def _apply_identity_link(
    pg_session: Session, plan: MergePlan, target: Contact
) -> bool:
    """Record the colliding email as an auth identity pointing at the merge
    target's B2C subject (when it has one). Idempotent against the partial
    unique index ``uq_identity_link_b2c_subject_id``.

    When the target has no b2c_subject_id yet, there is no subject to link
    to; we still upsert the email identity (b2c_subject_id NULL) so the
    record exists for a later sign-in to claim. ON CONFLICT DO NOTHING on
    the magic-email partial index keeps it idempotent.
    """
    subject = target.b2c_subject_id
    if subject is not None:
        stmt = (
            pg_insert(IdentityLink)
            .values(
                id=uuid.uuid4(),
                b2c_subject_id=subject,
                email=plan.email_normalized,
                email_normalized=plan.email_normalized,
                idp_name="dt_reconcile",
            )
            .on_conflict_do_update(
                index_elements=["b2c_subject_id"],
                index_where=text("b2c_subject_id IS NOT NULL"),
                set_={"email_normalized": plan.email_normalized},
                # Only fill an EMPTY email — never overwrite a B2C identity's
                # existing email_normalized. The column is NOT NULL (no server
                # default), so an established row always holds a non-empty
                # value; '' is the only "unset" state we backfill.
                where=IdentityLink.email_normalized == "",
            )
        )
        pg_session.execute(stmt)
        return True
    return False


def _apply_overwrite(pg_session: Session, plan: MergePlan) -> None:
    """DT-authoritative field overwrite: write every planned change directly
    onto the target (no ``IS NULL`` guard — DT is the record of truth, and
    ``merge_descriptive`` already restricted ``field_changes`` to columns
    where DT has a value that differs from core)."""
    if not plan.field_changes:
        return
    pg_session.execute(
        update(Contact)
        .where(Contact.id == plan.target_contact_id)
        .values(**plan.field_changes)
    )


def _repoint_loser_interests(pg_session: Session, plan: MergePlan) -> None:
    """Re-point the DT loser contact's OWN ``adopter_interest`` rows onto the
    merge target BEFORE the loser is deleted, so its interests are genuinely
    unioned rather than cascade-deleted with the stub.

    The loser already owns dt-keyed interest rows
    (``source_id='{loser_post_id}:{people_id3}'``); they hang off
    ``loser_contact_id``. We move each by ``contact_id`` UNLESS the target
    already carries the same ``people_id3`` (the union semantics — never
    duplicate an FPG the target already has; the orphaned loser row is left
    to cascade-delete with the stub). The (source_system, source_id) unique
    index is unaffected by contact_id, so the move is a plain UPDATE.
    """
    if plan.loser_contact_id is None:
        return
    target_people = {
        pid
        for (pid,) in pg_session.execute(
            select(AdopterInterest.people_id3).where(
                AdopterInterest.contact_id == plan.target_contact_id
            )
        ).all()
        if pid is not None
    }
    loser_interests = pg_session.execute(
        select(AdopterInterest).where(
            AdopterInterest.contact_id == plan.loser_contact_id
        )
    ).scalars().all()
    moved = 0
    for interest in loser_interests:
        if interest.people_id3 is not None and interest.people_id3 in target_people:
            # Target already has this FPG — don't duplicate; let the loser
            # row cascade-delete with the stub.
            continue
        pg_session.execute(
            update(AdopterInterest)
            .where(AdopterInterest.id == interest.id)
            .values(contact_id=plan.target_contact_id)
        )
        if interest.people_id3 is not None:
            target_people.add(interest.people_id3)
        moved += 1
    plan.interests_repointed = moved


def _repoint_history_and_assignment(
    pg_session: Session, plan: MergePlan
) -> None:
    """Move the DT loser contact's activity history + assignment onto the
    merge target so the surviving record carries the full timeline.

    * activity_log rows are re-pointed by ``contact_id``. The unique index
      is on (source_system, source_id), unaffected by contact_id, so this
      is a plain UPDATE and idempotent (a re-run finds nothing left to
      move).
    * contact_assignment is 1:1 (contact_id PK). ContactAssignment is
      DT-authoritative REPLACE for clean merges (design rule) — BUT never
      clobber a staff override. A clean-merge contact can still carry a
      staff assignment (assigning a facilitator does not set
      ``local_modified_after_import``, so skip_protected does not cover it).
      So we only replace when the target has no assignment OR its existing
      one is itself a ``dt_import`` row; a non-DT (staff) target assignment
      is preserved. We also guard the loser side to ``dt_import`` so we
      never carry a non-DT assignment across.
    """
    if plan.loser_contact_id is None:
        return
    res = pg_session.execute(
        update(ActivityLog)
        .where(ActivityLog.contact_id == plan.loser_contact_id)
        .values(contact_id=plan.target_contact_id)
    )
    plan.activity_repointed = int(res.rowcount or 0)

    loser_assignment = pg_session.execute(
        select(ContactAssignment).where(
            ContactAssignment.contact_id == plan.loser_contact_id
        )
    ).scalars().first()
    if loser_assignment is None or loser_assignment.assigned_by != "dt_import":
        return
    # Never clobber a staff override: only DT-replace when the target has no
    # assignment or its own is a dt_import row.
    target_assignment = pg_session.execute(
        select(ContactAssignment).where(
            ContactAssignment.contact_id == plan.target_contact_id
        )
    ).scalars().first()
    if (
        target_assignment is not None
        and target_assignment.assigned_by != "dt_import"
    ):
        return
    # DT-replace: drop the existing (dt_import) target assignment, then write
    # the DT one.
    pg_session.execute(
        delete(ContactAssignment).where(
            ContactAssignment.contact_id == plan.target_contact_id
        )
    )
    pg_session.execute(
        pg_insert(ContactAssignment)
        .values(
            contact_id=plan.target_contact_id,
            user_subject_id=loser_assignment.user_subject_id,
            assigned_by="dt_import",
        )
        .on_conflict_do_update(
            index_elements=["contact_id"],
            set_={
                "user_subject_id": loser_assignment.user_subject_id,
                "assigned_by": "dt_import",
            },
        )
    )
    pg_session.execute(
        delete(ContactAssignment).where(
            ContactAssignment.contact_id == plan.loser_contact_id,
            ContactAssignment.assigned_by == "dt_import",
        )
    )
    plan.assignment_repointed = True


def _merge_children(
    pg_session: Session, plan: MergePlan, fpg_ids: set[str]
) -> None:
    """Merge DT child tables onto the target per the design's per-category
    rules:

    * **ContactProfile** — DT-overwrite (upsert by ``contact_id``). None-
      valued DT keys are dropped from the ``DO UPDATE`` set so an
      unmappable/clamped DT enum keeps the existing core value instead of
      nulling it.
    * **AdopterInterest** — union: add DT interests whose key isn't already
      on the target; never touch the core's existing interests. DT
      interests whose ``people_id3`` is absent from ``fpg`` are SKIPPED —
      inserting them would violate the ``adopter_interest.people_id3`` FK
      and abort the apply (mirrors ``orchestrator._flush_interest_batch``).
    * **Consent** — most-restrictive: NO-OP. The DT ETL imports no consent
      acceptance rows and ``Consent`` rows are opt-IN records, so leaving
      the target's consent untouched cannot weaken a core opt-out (the
      safety requirement). See ``consent_most_restrictive`` for the rule.
    * **ContactAssignment / ActivityLog** — handled in
      ``_repoint_history_and_assignment`` (DT-authoritative replace +
      append) and ``_repoint_loser_interests``.
    """
    target_id = plan.target_contact_id
    if target_id is None:
        return

    # ContactProfile: DT overwrites the 1:1 profile row. Drop None-valued
    # keys so an unmappable/clamped DT enum (mapper returns None) keeps the
    # core value rather than nulling it.
    if plan.dt_profile:
        profile_set = {k: v for k, v in plan.dt_profile.items() if v is not None}
        if profile_set:
            pg_session.execute(
                pg_insert(ContactProfile)
                .values(id=uuid.uuid4(), contact_id=target_id, **profile_set)
                .on_conflict_do_update(
                    index_elements=["contact_id"],
                    set_=profile_set,
                )
            )

    # AdopterInterest: union. Only add DT FPGs not already on the target,
    # and only those whose people_id3 exists in fpg (FK safety).
    if plan.dt_interests:
        existing_people = {
            pid
            for (pid,) in pg_session.execute(
                select(AdopterInterest.people_id3).where(
                    AdopterInterest.contact_id == target_id
                )
            ).all()
            if pid is not None
        }
        dt_by_people = {
            interest["people_id3"]: interest for interest in plan.dt_interests
        }
        for people_id3 in interests_to_add(
            core_keys=existing_people, dt_keys=set(dt_by_people)
        ):
            if people_id3 not in fpg_ids:
                # FPG not present locally — the FK would abort the batch.
                # Skip (operators run sync_fpg before cutover; rare).
                logger.warning(
                    "track_a: skipping DT interest for missing FPG %s "
                    "(source_id %s)",
                    people_id3,
                    plan.source_id,
                )
                continue
            interest = dt_by_people[people_id3]
            pg_session.execute(
                pg_insert(AdopterInterest)
                .values(
                    id=uuid.uuid4(),
                    contact_id=target_id,
                    source_system=SOURCE_SYSTEM,
                    source_id=f"{plan.source_id}:{people_id3}",
                    **interest,
                )
                .on_conflict_do_nothing(
                    index_elements=["source_system", "source_id"],
                    index_where=text("source_id IS NOT NULL"),
                )
            )


def _adopt_dt_keys(pg_session: Session, plan: MergePlan) -> None:
    """Durable resolution: the target adopts the DT identity keys
    (``source_system='dt'``, ``source_id=<conflict.source_id>``) so the next
    hourly sync resolves it by ``(source_system, source_id)`` — the update
    path — and never re-collides on email. Must run AFTER the loser's
    history/assignment are re-pointed off it.

    Key-collision guard: the DT "loser" Contact already holds
    ``('dt', source_id)`` under the partial unique index
    ``uq_contacts_source_system_source_id``. The target can't adopt those
    keys while the loser still owns them, so we delete the loser stub first
    (its email was dropped to NULL on import and its children have already
    moved to the target). Cascades clean up any remaining child rows.
    """
    if (
        plan.loser_contact_id is not None
        and plan.loser_contact_id != plan.target_contact_id
    ):
        pg_session.execute(
            delete(Contact).where(Contact.id == plan.loser_contact_id)
        )
    pg_session.execute(
        update(Contact)
        .where(Contact.id == plan.target_contact_id)
        .values(source_system=SOURCE_SYSTEM, source_id=plan.source_id)
    )


def _resolve_conflict(pg_session: Session, plan: MergePlan) -> None:
    """Delete the MigrationConflict row by its natural key — the model has
    no status column, so delete-on-resolve is the convention. Safe and
    idempotent: a later ETL only re-creates it if the collision recurs."""
    pg_session.execute(
        delete(MigrationConflict).where(
            MigrationConflict.source_system == SOURCE_SYSTEM,
            MigrationConflict.source_id == plan.source_id,
            MigrationConflict.table_name == TABLE_NAME,
            MigrationConflict.conflict_type == CONFLICT_TYPE,
        )
    )


def _apply_one(pg_session: Session, plan: MergePlan, fpg_ids: set[str]) -> None:
    target = pg_session.get(Contact, plan.target_contact_id)
    if target is None:
        # Target vanished between planning and apply — leave the conflict.
        plan.status = "skip_missing_target"
        plan.reason = "merge target disappeared before apply"
        return
    plan.identity_linked = _apply_identity_link(pg_session, plan, target)
    _apply_overwrite(pg_session, plan)
    # Re-point the loser's OWN dt-keyed interests onto the target BEFORE both
    # the JSON union (so it dedups against them) and the loser delete (so they
    # are not cascade-lost).
    _repoint_loser_interests(pg_session, plan)
    _merge_children(pg_session, plan, fpg_ids)
    _repoint_history_and_assignment(pg_session, plan)
    _adopt_dt_keys(pg_session, plan)
    _resolve_conflict(pg_session, plan)


# ──────────────────────────────────────────────────────────────────────────
# Review-list emission
# ──────────────────────────────────────────────────────────────────────────


def _review_rows(result: ReconcileResult) -> list[dict[str, Any]]:
    # Amy's review list = every carve-out that needs a human decision before
    # any merge: ambiguous-name, open-match, protected (do_not_engage /
    # locally edited), and multi-collision. The ``disposition`` column tells
    # her which carve-out triggered.
    return [
        {
            "source_id": p.source_id,
            "email_normalized": p.email_normalized,
            "dt_display_name": p.dt_display_name or "",
            "local_display_name": p.target_display_name or "",
            "target_contact_id": str(p.target_contact_id)
            if p.target_contact_id
            else "",
            "disposition": p.status,
            "recommended_keep": p.recommended_keep or "",
            "reason": p.reason,
        }
        for p in result.for_review_list
    ]


def write_review_list(
    result: ReconcileResult, path: str
) -> int:
    """Write the review list for Amy — every disposition needing a human
    decision: ``review`` (ambiguous name), ``skip_open_match``,
    ``skip_protected`` (do_not_engage / locally edited), and
    ``skip_multi_collision``. Format inferred from the path suffix
    (``.json`` => JSON, else CSV). Returns the row count."""
    rows = _review_rows(result)
    if path.endswith(".json"):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2, sort_keys=True)
    else:
        fieldnames = [
            "source_id",
            "email_normalized",
            "dt_display_name",
            "local_display_name",
            "target_contact_id",
            "disposition",
            "recommended_keep",
            "reason",
        ]
        with open(path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    logger.info("track_a: wrote %d review rows to %s", len(rows), path)
    return len(rows)


# ──────────────────────────────────────────────────────────────────────────
# Orchestration
# ──────────────────────────────────────────────────────────────────────────


def reconcile(
    *,
    pg_session: Session,
    mysql_conn: Any,
    mode: Mode = "dry_run",
    dt_reader: DtReader = _default_dt_reader,
    review_path: str | None = None,
    decisions: Decisions | None = None,
) -> ReconcileResult:
    """Plan + (in production) apply the DT-authoritative duplicate_email merge.

    DRY-RUN (default): plan everything, write an ``etl_run`` audit row, emit
    the review list, then ROLL BACK so zero data rows change — but the
    audit row survives so an operator sees the would-be effect.

    PRODUCTION (``mode='production'``, i.e. ``--apply``): applies every
    auto-mergeable plan inside one ``outbox_suppressed`` scope (single
    bulk_imported summary event), commits, and still emits the review list.
    """
    result = plan_merges(
        pg_session=pg_session,
        mysql_conn=mysql_conn,
        dt_reader=dt_reader,
        mode=mode,
        decisions=decisions,
    )

    run_row = EtlRun(
        id=uuid.uuid4(),
        table_name="reconcile_duplicate_email",
        mode=mode,
        started_at=datetime.now(UTC),
    )
    pg_session.add(run_row)
    pg_session.flush()

    # Load FPG ids once (FK safety for the interest union — mirrors the
    # importer's ``_load_existing_fpg_ids``); reused across every plan.
    fpg_ids = _load_existing_fpg_ids(pg_session)
    # Snapshot the auto-mergeable plans before the loop: a per-contact
    # SAVEPOINT failure flips a plan's status to 'failed', which would
    # otherwise mutate ``result.to_merge`` mid-iteration.
    mergeable = list(result.to_merge)

    # Mutable so we can correct ``merged``/``failed`` after the loop to the
    # counts that actually committed (a per-contact SAVEPOINT failure flips a
    # plan to 'failed'); the single bulk_imported summary fires on scope exit.
    summary_meta: dict[str, Any] = {
        "mode": mode,
        "conflict_type": CONFLICT_TYPE,
        "planned": len(result.planned),
        "merged": len(mergeable),
        "review": len(result.to_review),
        "skipped": len(result.skipped),
    }

    async def _drive() -> None:
        async with outbox_suppressed(
            RECONCILE_LABEL,
            pg_session,  # type: ignore[arg-type]  # async-safe ContextVar; session is sync
            metadata=summary_meta,
        ):
            for plan in mergeable:
                # One SAVEPOINT per contact: a failure rolls back ONLY that
                # contact's writes, marks the plan failed (its conflict row
                # stays for Amy), and the loop continues. The outer
                # outbox_suppressed scope + final commit are untouched, so
                # the other plans still land and the single bulk_imported
                # summary still fires.
                try:
                    with pg_session.begin_nested():
                        _apply_one(pg_session, plan, fpg_ids)
                except Exception:  # noqa: BLE001 — isolate one bad contact
                    plan.status = "failed"
                    plan.reason = "apply failed; rolled back to savepoint"
                    logger.exception(
                        "track_a: apply failed for source_id %s; "
                        "rolled back this contact and continuing",
                        plan.source_id,
                    )
            # Correct the summary to what actually committed before the
            # single bulk_imported event fires on scope exit.
            summary_meta["merged"] = len(result.to_merge)
            summary_meta["failed"] = len(result.failed)

    asyncio.run(_drive())

    counts = result.counts()
    run_row.rows_in = counts["rows_in"]
    run_row.rows_out_inserted = (
        counts["rows_out_inserted"] if mode == "production" else 0
    )
    run_row.rows_out_skipped = counts["rows_out_skipped"]
    run_row.rows_in_conflict = counts["rows_in_review"]
    run_row.ended_at = datetime.now(UTC)
    run_row.notes = (
        f"merged={len(result.to_merge)} review={len(result.to_review)} "
        f"skipped={len(result.skipped)} failed={len(result.failed)} "
        f"mode={mode}"
    )
    pg_session.flush()

    if review_path is not None:
        write_review_list(result, review_path)

    if mode == "production":
        pg_session.commit()
    else:
        # Dry-run: discard every data write (merges + the suppressed summary
        # event), then re-add ONLY the audit row so the rehearsal still
        # records what would have happened. Net DB change = one etl_run row.
        snapshot = {
            "table_name": run_row.table_name,
            "mode": run_row.mode,
            "started_at": run_row.started_at,
            "ended_at": run_row.ended_at,
            "rows_in": run_row.rows_in,
            "rows_out_inserted": run_row.rows_out_inserted,
            "rows_out_skipped": run_row.rows_out_skipped,
            "rows_in_conflict": run_row.rows_in_conflict,
            "notes": run_row.notes,
        }
        pg_session.rollback()
        pg_session.add(EtlRun(id=uuid.uuid4(), **snapshot))
        pg_session.commit()

    return result


def run(
    *,
    mysql_url: str,
    postgres_url: str,
    mode: Mode = "dry_run",
    review_path: str | None = None,
    decisions: Decisions | None = None,
) -> ReconcileResult:
    """Open both engines and reconcile. The MySQL engine is only
    ``.connect()``-ed; the gated reader does the actual queries. This is
    the operator entry point — never run with ``mode='production'`` against
    prod without an approved review list."""
    mysql_engine: Engine = open_engine(mysql_url)
    pg_engine: Engine = create_engine(postgres_url, future=True)
    SessionLocal = sessionmaker(pg_engine, expire_on_commit=False, autoflush=False)
    try:
        with SessionLocal() as pg_session, mysql_engine.connect() as mysql_conn:
            return reconcile(
                pg_session=pg_session,
                mysql_conn=mysql_conn,
                mode=mode,
                review_path=review_path,
                decisions=decisions,
            )
    finally:
        pg_engine.dispose()
        mysql_engine.dispose()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="track_a_duplicate_email",
        description="Reconcile DT duplicate_email migration conflicts.",
    )
    parser.add_argument("--mysql-url", required=True)
    parser.add_argument("--postgres-url", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes (production mode). Omitted => DRY-RUN (default).",
    )
    parser.add_argument(
        "--review-out",
        default=None,
        help="Path for the ambiguous-match review list (.csv or .json).",
    )
    parser.add_argument(
        "--decisions",
        default=None,
        help=(
            "Path to an operator decisions JSON (force_merge / multi_keep) "
            "fed back in so --apply honors reviewer calls."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    mode: Mode = "production" if args.apply else "dry_run"
    decisions = load_decisions(args.decisions) if args.decisions else None
    result = run(
        mysql_url=args.mysql_url,
        postgres_url=args.postgres_url,
        mode=mode,
        review_path=args.review_out,
        decisions=decisions,
    )
    logger.info(
        "track_a duplicate_email: mode=%s planned=%d merged=%d review=%d skipped=%d",
        mode,
        len(result.planned),
        len(result.to_merge),
        len(result.to_review),
        len(result.skipped),
    )
    return 0


__all__ = [
    "Decisions",
    "MergePlan",
    "ReconcileResult",
    "load_decisions",
    "main",
    "names_look_like_same_person",
    "plan_merges",
    "reconcile",
    "run",
    "write_review_list",
]


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
