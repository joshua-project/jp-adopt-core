"""Track A — reconcile ``duplicate_email`` migration conflicts.

===================== STATUS: DIAGNOSTICS-ONLY =====================
The merge WRITE path (``--apply``) is HARD-GATED and raises unless an
explicit ``allow_unsafe_merge`` override is passed. The dry-run diagnostics
and the ambiguous-match review list are fully functional and are the only
supported outputs today.

It is gated because the agreed policy is now **DT-authoritative** (Amy
curates contacts in DT, so DT values are canonical) — the OPPOSITE merge
direction from the backfill-only/forms-authoritative logic implemented
below. Three redesign items must land before the gate is lifted:

  (1) DT is authoritative — DT values are CANONICAL, not backfill-only.
      The current ``_apply_backfill`` only fills empty target columns; a
      DT-authoritative merge must let curated DT values win.
  (2) Merge ALL DT child tables. Today only ActivityLog + ContactAssignment
      are re-pointed; ContactProfile / AdopterInterest / Consent /
      Enrollment / etc. on the DT loser are STRANDED.
  (3) Durable conflict resolution. Deleting the MigrationConflict row is not
      durable — the hourly cron re-detects the same email collision and
      re-creates the conflict, so resolution must persist a real decision.
=========================================================================================

Backlog in prod (2026-06-18): 210 ``duplicate_email`` rows on
``table_name='contacts'``.

Why they exist
--------------
DT permits the same email on multiple contacts; the new system enforces a
partial unique index (``uq_contacts_email_normalized`` WHERE
email_normalized IS NOT NULL). During the main ETL, when a second DT
contact carried an email already owned by an existing local contact, the
importer kept the row but DROPPED the colliding email to NULL and recorded
a ``duplicate_email`` conflict (``orchestrator._flush_contact_batch``,
source_value={'email_normalized': '<addr>'}, local_value=null).

That conflict row is LOSSY — it carries neither the DT payload nor the
merge-target contact id. So to reconcile one row we must:

  (a) find the existing local **merge target** Contact by
      ``email_normalized == source_value['email_normalized']`` (the contact
      that kept the email), and
  (b) RE-READ the full DT contact by ``source_id`` (= the conflict's
      ``source_id`` = wp_posts.ID) via the gated DT reader.

The merge
---------
The DT "loser" contact already exists locally as its own Contact row
(``source_system='dt'``, ``source_id=<conflict.source_id>``) with
``email_normalized=NULL``. Merging it into the target means:

  1. **identity_link** — record the colliding email as an auth identity
     pointing at the merge target's B2C subject (when the target has one),
     so a future login via that email resolves to the surviving contact.
     Idempotent against ``uq_identity_link_b2c_subject_id`` (partial,
     WHERE b2c_subject_id IS NOT NULL).
  2. **backfill empty fields only** on the merge target from the freshly
     re-read DT contact — never overwrite a non-empty local value.
  3. **re-point activity history + assignment** of the DT loser contact
     onto the merge target so the surviving record carries the full
     timeline.
  4. **resolve the conflict** by deleting the MigrationConflict row (the
     model has no status column — delete-on-resolve is the convention).

Ambiguity / review list
------------------------
Same email + clearly different name => possibly a shared/family inbox
rather than the same person. Those are NOT auto-merged; they are written
to a REVIEW LIST (CSV/JSON) for Amy to confirm before ``--apply``.

Safety
------
* DEFAULTS TO DRY-RUN. ``--apply`` (or ``mode='production'``) is required
  to write; dry-run rolls back every data write but still surfaces an
  ``etl_run`` audit row + the would-be conflict deltas (mirrors
  ``orchestrator.run_etl``).
* Bulk writes go through ``outbox_suppressed`` — one
  ``jp.adopt.v1.bulk_imported`` summary event, never per-row Outbox.
* Idempotent: ON CONFLICT upserts + delete-by-natural-key, so
  dry-run-then-apply or apply-twice is safe.
* DT MySQL is credential-gated and unavailable in CI — the DT reader is
  injectable (``dt_reader=``) so tests mock it; the real run is
  operator-gated.
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
from jp_adopt_etl.mappers.contacts import map_contact
from jp_adopt_etl.mappers.status import Mode

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

# Contact columns we are willing to backfill from DT when the local value
# is empty. We deliberately EXCLUDE adopter_status / facilitator_status —
# those mutate only via the workflow router (AGENTS.md), never here. We
# also exclude email_normalized: the merge target already owns the email,
# and the DT loser's email is the one we are folding in.
_BACKFILLABLE_FIELDS = (
    "phone",
    "origin",
    "country_code",
)


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
    # Fields that would be backfilled onto the target {column: value}.
    backfill: dict[str, Any] = field(default_factory=dict)
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

        # Backfill plan: only columns where the target value is empty AND
        # the DT value is non-empty.
        for col in _BACKFILLABLE_FIELDS:
            local_val = getattr(target, col, None)
            dt_val = dt_kwargs.get(col)
            if (local_val is None or local_val == "") and dt_val not in (None, ""):
                plan.backfill[col] = dt_val

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


def _apply_backfill(pg_session: Session, plan: MergePlan) -> None:
    """Backfill empty target columns. Guarded so a concurrent write that
    filled a column since planning still wins (``col IS NULL``)."""
    if not plan.backfill:
        return
    conditions = " AND ".join(f"{col} IS NULL" for col in plan.backfill)
    pg_session.execute(
        update(Contact)
        .where(Contact.id == plan.target_contact_id)
        .where(text(conditions))
        .values(**plan.backfill)
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
    _apply_backfill(pg_session, plan)
    _repoint_history_and_assignment(pg_session, plan)
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
    allow_unsafe_merge: bool = False,
) -> ReconcileResult:
    """Plan + (in production) apply duplicate_email merges.

    DRY-RUN (default): plan everything, write an ``etl_run`` audit row, emit
    the review list, then ROLL BACK so zero data rows change — but the
    audit row survives so an operator sees the would-be effect.

    PRODUCTION (``mode='production'``, i.e. ``--apply``): GATED. Raises a
    ``RuntimeError`` unless ``allow_unsafe_merge=True`` is passed, because
    the merge is pending the DT-authoritative redesign (see the module
    docstring's STATUS: DIAGNOSTICS-ONLY block). When the override is set,
    applies every auto-mergeable plan inside one ``outbox_suppressed`` scope
    (single bulk_imported summary event), commits, and still emits the
    review list.
    """
    if mode == "production" and not allow_unsafe_merge:
        raise RuntimeError(
            "Track A merge --apply is gated: pending DT-authoritative merge "
            "redesign (re-point ALL DT child tables; durable conflict "
            "resolution). See module docstring. Pass --allow-unsafe-merge "
            "(allow_unsafe_merge=True) only to deliberately exercise the "
            "unsafe write path."
        )
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
    allow_unsafe_merge: bool = False,
) -> ReconcileResult:
    """Open both engines and reconcile. The MySQL engine is only
    ``.connect()``-ed; the gated reader does the actual queries. This is
    the operator entry point — never run with ``mode='production'`` against
    prod without an approved review list. ``--apply`` is additionally gated
    behind ``allow_unsafe_merge`` (see the module docstring)."""
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
                allow_unsafe_merge=allow_unsafe_merge,
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
        help="Write changes (production mode). Omitted => DRY-RUN (default). "
        "GATED: also requires --allow-unsafe-merge (the merge is pending the "
        "DT-authoritative redesign — see the module docstring).",
    )
    parser.add_argument(
        "--allow-unsafe-merge",
        action="store_true",
        default=False,
        help="Override the --apply gate and run the UNSAFE merge write path. "
        "Pending the DT-authoritative redesign; do not use against prod.",
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
        allow_unsafe_merge=args.allow_unsafe_merge,
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
