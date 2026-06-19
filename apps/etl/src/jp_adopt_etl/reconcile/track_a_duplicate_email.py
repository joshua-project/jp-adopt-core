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
from jp_adopt_etl.reconcile.track_a_merge import (
    interests_to_add,
    merge_descriptive,
)

logger = logging.getLogger(__name__)

CONFLICT_TYPE = "duplicate_email"
TABLE_NAME = "contacts"
SOURCE_SYSTEM = "dt"
RECONCILE_LABEL = "dt_reconcile:duplicate_email"

# "Open" match statuses — a contact with a match in any of these is in
# active in-core triage and must NOT have its workflow status reset by the
# DT-authoritative merge. This is the same not-yet-terminal set the
# ``uq_match_open_per_interest`` partial unique index uses (models.py); the
# terminal statuses (declined / completed / withdrawn / sent_back) are NOT
# open. A Match has no direct contact FK — it hangs off adopter_interest —
# so the predicate joins Match → AdopterInterest by contact_id.
OPEN_MATCH_STATUSES = ("recommended", "accepted", "active", "triage")

# Contact columns the DT-authoritative merge overwrites onto the target
# where DT has a value (``merge_descriptive`` keeps the core value only
# where DT is empty). adopter_status / facilitator_status are INCLUDED:
# the merge is DT-authoritative and the open-match pre-check has already
# excluded anything under live in-core triage, so a direct status write is
# safe here (consistent with the ETL importer, which also writes status
# directly). email_normalized is excluded — the target already owns the
# email; the DT loser's email is folded in via the identity link + key
# adoption, not a column overwrite.
_MERGE_FIELDS = (
    "display_name",
    "phone",
    "origin",
    "country_code",
    "adopter_status",
    "facilitator_status",
)

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
    assignment_repointed: bool = False
    identity_linked: bool = False
    # Disposition: 'merge' | 'review' | 'skip_open_match' | 'skip_missing_target'.
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
    def for_review_list(self) -> list[MergePlan]:
        """Cases needing an Amy decision: ambiguous names + open-match
        skips. (``skip_missing_target`` is data hygiene, not a review item.)"""
        return [
            p
            for p in self.planned
            if p.status in ("review", "skip_open_match")
        ]

    def counts(self) -> dict[str, int]:
        return {
            "rows_in": len(self.planned),
            "rows_out_inserted": len(self.to_merge),
            "rows_out_skipped": len(self.skipped) + len(self.to_review),
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
) -> ReconcileResult:
    """Decide what to do with every ``duplicate_email`` conflict.

    No writes. Each conflict becomes a :class:`MergePlan` whose ``status``
    is ``merge`` (clear, auto-mergeable), ``review`` (ambiguous name —
    needs Amy), or ``skip_missing_target`` (the merge target no longer
    carries the email; nothing to merge into).
    """
    result = ReconcileResult()
    for conflict in _load_conflicts(pg_session):
        source_id = conflict.source_id
        raw_email = (conflict.source_value or {}).get("email_normalized")
        email = normalize_email(raw_email) if raw_email else None
        plan = MergePlan(source_id=source_id, email_normalized=email or "")
        if not email:
            plan.status = "skip_missing_target"
            plan.reason = "conflict row has no email_normalized"
            result.planned.append(plan)
            continue

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
        # violates the contacts status CHECK constraints.
        core_vals = {col: getattr(target, col, None) for col in _MERGE_FIELDS}
        dt_vals = {col: dt_kwargs.get(col) for col in _MERGE_FIELDS}
        for col in _STATUS_FIELDS:
            if dt_vals.get(col) in _INVALID_STATUS_VALUES:
                dt_vals[col] = None
        plan.field_changes = merge_descriptive(core=core_vals, dt=dt_vals)

        # Child-table inputs from the same DT postmeta pivot. Carried on the
        # plan so the apply step merges them without re-reading DT.
        dt_meta = pivot_postmeta(meta_rows)
        plan.dt_profile = map_contact_profile(dt_meta)
        plan.dt_interests = parse_fpg_submission_data(
            dt_meta.get("fpg_submission_data")
        )

        # Ambiguity gate: same email but mismatched name => possible shared
        # inbox. Route to the review list instead of auto-merging.
        if not names_look_like_same_person(
            plan.dt_display_name, plan.target_display_name
        ):
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


def _repoint_history_and_assignment(
    pg_session: Session, plan: MergePlan
) -> None:
    """Move the DT loser contact's activity history + assignment onto the
    merge target so the surviving record carries the full timeline.

    * activity_log rows are re-pointed by ``contact_id``. The unique index
      is on (source_system, source_id), unaffected by contact_id, so this
      is a plain UPDATE and idempotent (a re-run finds nothing left to
      move).
    * contact_assignment is 1:1 (contact_id PK). We only move a
      DT-imported assignment, and only when the target has none — never
      clobber a staff override on either side (assigned_by guard, mirrors
      ``orchestrator._flush_assignment_batch``).
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
    target_has = pg_session.execute(
        select(ContactAssignment.contact_id).where(
            ContactAssignment.contact_id == plan.target_contact_id
        )
    ).first()
    if target_has is not None:
        return
    pg_session.execute(
        pg_insert(ContactAssignment)
        .values(
            contact_id=plan.target_contact_id,
            user_subject_id=loser_assignment.user_subject_id,
            assigned_by="dt_import",
        )
        .on_conflict_do_nothing(index_elements=["contact_id"])
    )
    pg_session.execute(
        delete(ContactAssignment).where(
            ContactAssignment.contact_id == plan.loser_contact_id,
            ContactAssignment.assigned_by == "dt_import",
        )
    )
    plan.assignment_repointed = True


def _merge_children(pg_session: Session, plan: MergePlan) -> None:
    """Merge DT child tables onto the target per the design's per-category
    rules:

    * **ContactProfile** — DT-overwrite (upsert by ``contact_id``).
    * **AdopterInterest** — union: add DT interests whose key isn't already
      on the target; never touch the core's existing interests.
    * **Consent** — most-restrictive: NO-OP. The DT ETL imports no consent
      acceptance rows and ``Consent`` rows are opt-IN records, so leaving
      the target's consent untouched cannot weaken a core opt-out (the
      safety requirement). See ``consent_most_restrictive`` for the rule.
    * **ContactAssignment / ActivityLog** — handled in
      ``_repoint_history_and_assignment`` (DT-authoritative replace +
      append).
    """
    target_id = plan.target_contact_id
    if target_id is None:
        return

    # ContactProfile: DT overwrites the 1:1 profile row.
    if plan.dt_profile:
        pg_session.execute(
            pg_insert(ContactProfile)
            .values(id=uuid.uuid4(), contact_id=target_id, **plan.dt_profile)
            .on_conflict_do_update(
                index_elements=["contact_id"],
                set_=plan.dt_profile,
            )
        )

    # AdopterInterest: union. Only add DT FPGs not already on the target.
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
    if plan.loser_contact_id is not None and plan.loser_contact_id != plan.target_contact_id:
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


def _apply_one(pg_session: Session, plan: MergePlan) -> None:
    target = pg_session.get(Contact, plan.target_contact_id)
    if target is None:
        # Target vanished between planning and apply — leave the conflict.
        plan.status = "skip_missing_target"
        plan.reason = "merge target disappeared before apply"
        return
    plan.identity_linked = _apply_identity_link(pg_session, plan, target)
    _apply_overwrite(pg_session, plan)
    _merge_children(pg_session, plan)
    _repoint_history_and_assignment(pg_session, plan)
    _adopt_dt_keys(pg_session, plan)
    _resolve_conflict(pg_session, plan)


# ──────────────────────────────────────────────────────────────────────────
# Review-list emission
# ──────────────────────────────────────────────────────────────────────────


def _review_rows(result: ReconcileResult) -> list[dict[str, Any]]:
    # Amy's review list = ambiguous-name cases AND open-match skips. Both
    # need a human decision before any merge; the ``disposition`` column
    # tells her which carve-out triggered.
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
            "reason": p.reason,
        }
        for p in result.for_review_list
    ]


def write_review_list(
    result: ReconcileResult, path: str
) -> int:
    """Write the review list for Amy (ambiguous-name + open-match cases).
    Format inferred from the path suffix (``.json`` => JSON, else CSV).
    Returns the row count."""
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
    )

    run_row = EtlRun(
        id=uuid.uuid4(),
        table_name="reconcile_duplicate_email",
        mode=mode,
        started_at=datetime.now(UTC),
    )
    pg_session.add(run_row)
    pg_session.flush()

    async def _drive() -> None:
        async with outbox_suppressed(
            RECONCILE_LABEL,
            pg_session,  # type: ignore[arg-type]  # async-safe ContextVar; session is sync
            metadata={
                "mode": mode,
                "conflict_type": CONFLICT_TYPE,
                "planned": len(result.planned),
                "merged": len(result.to_merge),
                "review": len(result.to_review),
                "skipped": len(result.skipped),
            },
        ):
            for plan in result.to_merge:
                _apply_one(pg_session, plan)

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
        f"skipped={len(result.skipped)} mode={mode}"
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    mode: Mode = "production" if args.apply else "dry_run"
    result = run(
        mysql_url=args.mysql_url,
        postgres_url=args.postgres_url,
        mode=mode,
        review_path=args.review_out,
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
    "MergePlan",
    "ReconcileResult",
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
