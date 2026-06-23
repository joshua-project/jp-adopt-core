"""Track B reconciliation: clear ``assignee_no_subject`` conflicts.

Background
----------
The main ETL imports DT ``assigned_to`` postmeta into ``contact_assignment``
by resolving ``user-<wp_user_id>`` to a B2C subject via
``staff_identity_link.b2c_subject_id``. Staff who have not yet signed into
B2C have a NULL ``b2c_subject_id``, so their assignments are SKIPPED and an
``assignee_no_subject`` MigrationConflict is recorded (one row per
assignment — 246 in prod). The conflict row stores only
``source_value={"assigned_to": "user-N"}`` and ``source_id=<post_id>``; it
carries no contact id and no subject.

The fix is small because the distinct set of ``assigned_to`` handles is tiny
relative to 246 rows: backfill ``b2c_subject_id`` onto the handful of
``staff_identity_link`` rows (keyed by ``dt_user_id``), then re-resolve each
assignment and delete the now-satisfied conflict rows.

This module resolves PURELY from data already in core plus an operator
mapping ``{dt_user_handle -> core subject}``. No DT MySQL re-read is needed
for Track B (the conflict already carries the handle, and the contact is
found locally by ``source_id``). A gated DT-reader hook is left as a stub for
the rare case an operator wants to confirm the live handle, but it is OFF by
default and never invoked in the resolve path.

Safety
------
* DRY-RUN BY DEFAULT. ``--apply`` (``mode="production"``) is required to
  write. In dry-run we compute and PRINT every would-be change and leave
  zero net DB change.
* Bulk writes run inside ``outbox_suppressed(...)`` → one
  ``jp.adopt.v1.bulk_imported`` summary event, never per-row Outbox.
* Idempotent: ``b2c_subject_id`` backfill is an ON CONFLICT upsert keyed by
  ``dt_user_id`` (the partial unique on ``b2c_subject_id`` guards two handles
  claiming one subject); contact_assignment re-resolve mirrors the
  orchestrator's ``assigned_by='dt_import'`` guard; conflict rows are removed
  by natural key, and ``_record_conflict`` is ON CONFLICT DO NOTHING, so a
  later ETL run only re-creates a conflict if the condition still holds.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jp_adopt_api.email_utils import normalize_email
from jp_adopt_api.models import (
    ContactAssignment,
    IdentityLink,
    MigrationConflict,
    StaffIdentityLink,
)
from jp_adopt_api.outbox_suppression import outbox_suppressed
from sqlalchemy import create_engine, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql import text

from jp_adopt_etl.mappers.assignment import parse_assigned_user_id

TABLE_NAME = "contact_assignment"
CONFLICT_TYPE = "assignee_no_subject"
SOURCE_SYSTEM = "dt"

# Sentinel mapping value marking a handle as a DT service account (e.g. the
# api-forms intake bot, wp_user 2) — no human owner. Such a handle's
# conflicts are DELETED and NO assignment is created. JSON ``null`` is
# accepted as an equivalent shorthand.
SERVICE_SENTINEL = "__service__"

DEFAULT_POSTGRES_URL = "postgresql+psycopg2://jp_adopt:jp_adopt@127.0.0.1:5434/jp_adopt"


# ──────────────────────────────────────────────────────────────────────────
# Mapping config
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SubjectMapping:
    """Operator-supplied ``dt_user_handle -> core subject`` mapping.

    Keys are the raw DT ``assigned_to`` handles exactly as stored in the
    conflict (``"user-12"``). Each value describes the core staff subject to
    link the handle to.
    """

    by_handle: dict[str, MappedSubject]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SubjectMapping:
        by_handle: dict[str, MappedSubject] = {}
        for handle, spec in raw.items():
            by_handle[str(handle)] = MappedSubject.from_spec(handle, spec)
        return cls(by_handle=by_handle)

    @classmethod
    def from_file(cls, path: str | Path) -> SubjectMapping:
        data = json.loads(Path(path).read_text())
        # Allow either a flat {handle: subject} object or {"mapping": {...}}.
        raw = data.get("mapping", data) if isinstance(data, dict) else data
        return cls.from_dict(raw)

    def get(self, handle: str) -> MappedSubject | None:
        return self.by_handle.get(handle)


@dataclass(frozen=True)
class MappedSubject:
    """One entry from the operator mapping, normalized."""

    handle: str
    subject_id: str
    email: str | None = None
    display_name: str | None = None
    # When True, also create/refresh a general ``identity_link`` row (auth
    # login) for this subject. Defaults False — Track B only needs the staff
    # link to clear the conflict.
    link_auth_identity: bool = False
    # When True the handle is a DT service account (no human owner): its
    # conflicts are cleared with NO assignment and NO subject backfill.
    is_service: bool = False

    @classmethod
    def from_spec(cls, handle: str, spec: Any) -> MappedSubject:
        # JSON null or the ``__service__`` sentinel marks a service account.
        if spec is None or spec == SERVICE_SENTINEL:
            return cls(handle=str(handle), subject_id="", is_service=True)
        if isinstance(spec, str):
            return cls(handle=str(handle), subject_id=spec)
        if isinstance(spec, dict):
            if spec.get("service"):
                return cls(handle=str(handle), subject_id="", is_service=True)
            subject = spec.get("subject") or spec.get("oid") or spec.get("subject_id")
            if not subject:
                raise ValueError(
                    f"mapping for {handle!r} must provide 'subject' (or 'oid')"
                )
            return cls(
                handle=str(handle),
                subject_id=str(subject),
                email=spec.get("email"),
                display_name=spec.get("display_name"),
                link_auth_identity=bool(spec.get("link_auth_identity", False)),
            )
        raise ValueError(f"mapping for {handle!r} must be a string or object")

    @property
    def wp_user_id(self) -> str | None:
        """The numeric DT user id parsed from the ``user-N`` handle."""
        return parse_assigned_user_id(self.handle)


# ──────────────────────────────────────────────────────────────────────────
# Diagnostics
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class HandleStat:
    handle: str
    wp_user_id: str | None
    count: int
    # post_ids (= conflict source_ids) carrying this handle.
    source_ids: list[str] = field(default_factory=list)


def distinct_handles(session: Session) -> list[HandleStat]:
    """Report the DISTINCT set of ``assigned_to`` handles among the open
    ``assignee_no_subject`` conflicts, with per-handle counts and the
    conflict source_ids. This is the key operator diagnostic: it shows how
    FEW distinct handles back the (large) conflict count.
    """
    rows = session.execute(
        select(MigrationConflict.source_id, MigrationConflict.source_value).where(
            MigrationConflict.source_system == SOURCE_SYSTEM,
            MigrationConflict.table_name == TABLE_NAME,
            MigrationConflict.conflict_type == CONFLICT_TYPE,
        )
    ).all()

    by_handle: dict[str, HandleStat] = {}
    for source_id, source_value in rows:
        handle = (source_value or {}).get("assigned_to")
        key = handle if handle is not None else "<missing>"
        stat = by_handle.get(key)
        if stat is None:
            stat = HandleStat(
                handle=key,
                wp_user_id=parse_assigned_user_id(handle) if handle else None,
                count=0,
            )
            by_handle[key] = stat
        stat.count += 1
        stat.source_ids.append(source_id)

    # Most-frequent first so the operator sees the biggest wins on top.
    return sorted(by_handle.values(), key=lambda s: (-s.count, s.handle))


# ──────────────────────────────────────────────────────────────────────────
# Reconcile plan / result
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class ReconcilePlan:
    """What a reconcile run WOULD (dry-run) or DID (apply) do."""

    total_conflicts: int = 0
    handles: list[HandleStat] = field(default_factory=list)
    # handle -> subject we will link
    mapped_handles: dict[str, str] = field(default_factory=dict)
    # handles present in conflicts but absent from the operator mapping
    unmapped_handles: list[str] = field(default_factory=list)
    # handles mapped to a service account (null/sentinel) → conflicts cleared,
    # no assignment created
    service_handles: list[str] = field(default_factory=list)
    staff_links_to_set: int = 0
    identity_links_to_set: int = 0
    assignments_to_resolve: int = 0
    conflicts_to_clear: int = 0
    # service-account conflict rows cleared with no assignment created
    service_handles_cleared: int = 0
    # assignments deliberately NOT created because the owner is a service
    # account (== service_handles_cleared; named for the discard semantics)
    assignments_discarded: int = 0
    # conflicts whose contact could not be found locally (left in place)
    conflicts_unresolvable: list[str] = field(default_factory=list)
    applied: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "total_conflicts": self.total_conflicts,
            "distinct_handles": len(self.handles),
            "mapped_handles": len(self.mapped_handles),
            "service_handles": len(self.service_handles),
            "unmapped_handles": len(self.unmapped_handles),
            "staff_links_to_set": self.staff_links_to_set,
            "identity_links_to_set": self.identity_links_to_set,
            "assignments_to_resolve": self.assignments_to_resolve,
            "conflicts_to_clear": self.conflicts_to_clear,
            "service_handles_cleared": self.service_handles_cleared,
            "assignments_discarded": self.assignments_discarded,
            "conflicts_unresolvable": len(self.conflicts_unresolvable),
            "applied": self.applied,
        }


def _load_contact_id_by_source_id(
    session: Session, source_ids: list[str]
) -> dict[str, uuid.UUID]:
    """Map DT post_id (conflict source_id) -> local Contact.id."""
    if not source_ids:
        return {}
    from jp_adopt_api.models import Contact

    rows = session.execute(
        select(Contact.source_id, Contact.id).where(
            Contact.source_system == SOURCE_SYSTEM,
            Contact.source_id.in_(source_ids),
        )
    ).all()
    return {r.source_id: r.id for r in rows}


def build_plan(session: Session, mapping: SubjectMapping) -> ReconcilePlan:
    """Compute the reconcile plan from current DB state + operator mapping.

    Pure read: no writes. Used by both dry-run (print + stop) and apply
    (print + execute).
    """
    handles = distinct_handles(session)
    plan = ReconcilePlan(handles=handles)
    plan.total_conflicts = sum(s.count for s in handles)

    # Which handles can we map, and which contacts back them?
    all_source_ids: list[str] = []
    for stat in handles:
        all_source_ids.extend(stat.source_ids)
    contact_by_source = _load_contact_id_by_source_id(session, all_source_ids)

    distinct_subjects: set[str] = set()
    for stat in handles:
        mapped = mapping.get(stat.handle)
        if mapped is not None and mapped.is_service:
            # Service account (no human owner): clear its conflicts, create no
            # assignment. Not unmapped — intentionally resolved.
            plan.service_handles.append(stat.handle)
            plan.service_handles_cleared += stat.count
            plan.assignments_discarded += stat.count
            continue
        if mapped is None or stat.wp_user_id is None:
            plan.unmapped_handles.append(stat.handle)
            continue
        plan.mapped_handles[stat.handle] = mapped.subject_id
        distinct_subjects.add(stat.wp_user_id)
        if mapped.link_auth_identity and mapped.email:
            plan.identity_links_to_set += 1
        # Per-conflict resolvability.
        for sid in stat.source_ids:
            if sid in contact_by_source:
                plan.assignments_to_resolve += 1
                plan.conflicts_to_clear += 1
            else:
                plan.conflicts_unresolvable.append(sid)

    # One staff_identity_link upsert per distinct wp_user_id (not per conflict).
    plan.staff_links_to_set = len(distinct_subjects)
    return plan


# ──────────────────────────────────────────────────────────────────────────
# Writers (mutating — only reached on apply, inside outbox_suppressed)
# ──────────────────────────────────────────────────────────────────────────


def _upsert_staff_subject(
    session: Session, *, wp_user_id: str, mapped: MappedSubject
) -> None:
    """Idempotently set ``b2c_subject_id`` on the StaffIdentityLink keyed by
    ``dt_user_id``. Mirrors ``orchestrator.import_users`` ON CONFLICT shape.

    If no link row exists yet (staff never imported), insert a minimal one;
    on re-run the ON CONFLICT updates the subject. The partial unique on
    ``b2c_subject_id`` (WHERE b2c_subject_id IS NOT NULL) guards two handles
    claiming the same subject.
    """
    email = (mapped.email or "").strip()
    set_: dict[str, Any] = {"b2c_subject_id": mapped.subject_id}
    if email:
        set_["email"] = email
        set_["email_normalized"] = normalize_email(email)
    if mapped.display_name:
        set_["display_name"] = mapped.display_name

    session.execute(
        pg_insert(StaffIdentityLink)
        .values(
            id=uuid.uuid4(),
            dt_user_id=wp_user_id,
            b2c_subject_id=mapped.subject_id,
            email=email,
            email_normalized=normalize_email(email) if email else "",
            display_name=mapped.display_name,
            status="active",
            source_system="dt",
        )
        .on_conflict_do_update(
            index_elements=["dt_user_id"],
            set_=set_,
            # Only fill an UNSET subject — never clobber a real B2C identity
            # that the staff member already established by signing in.
            where=StaffIdentityLink.b2c_subject_id.is_(None),
        )
    )


def _upsert_identity_link(session: Session, *, mapped: MappedSubject) -> None:
    """Idempotently create the general auth ``identity_link`` for a subject.

    Targets the partial unique ``uq_identity_link_b2c_subject_id`` (WHERE
    b2c_subject_id IS NOT NULL) — the predicate MUST be passed to ON CONFLICT
    or Postgres raises. Only invoked when the mapping opts in
    (``link_auth_identity`` + email present).
    """
    email = (mapped.email or "").strip()
    if not email:
        return
    session.execute(
        pg_insert(IdentityLink)
        .values(
            id=uuid.uuid4(),
            b2c_subject_id=mapped.subject_id,
            email=email,
            email_normalized=normalize_email(email),
            # TODO(confirm idp_name): auth.py issuer-regex dispatch governs this
            idp_name="b2c",
        )
        .on_conflict_do_update(
            index_elements=["b2c_subject_id"],
            index_where=text("b2c_subject_id IS NOT NULL"),
            set_={"email": email, "email_normalized": normalize_email(email)},
        )
    )


def _resolve_assignment(
    session: Session, *, contact_id: uuid.UUID, subject_id: str
) -> bool:
    """Place/refresh the contact_assignment, preserving the staff-override
    guard (only update rows whose ``assigned_by='dt_import'``). Returns True
    if a row was written, False if a staff override blocked it.
    """
    stmt = (
        pg_insert(ContactAssignment)
        .values(
            contact_id=contact_id,
            user_subject_id=subject_id,
            assigned_by="dt_import",
        )
        .on_conflict_do_update(
            index_elements=["contact_id"],
            set_={"user_subject_id": subject_id},
            where=ContactAssignment.assigned_by == "dt_import",
        )
        .returning(ContactAssignment.contact_id)
    )
    return session.execute(stmt).one_or_none() is not None


def _delete_conflict(session: Session, *, source_id: str) -> None:
    """Remove the resolved conflict by natural key (delete-on-resolve — the
    model has no status column). ``_record_conflict`` is ON CONFLICT DO
    NOTHING, so a later ETL run re-creates it only if still unresolved."""
    session.execute(
        delete(MigrationConflict).where(
            MigrationConflict.source_system == SOURCE_SYSTEM,
            MigrationConflict.source_id == source_id,
            MigrationConflict.table_name == TABLE_NAME,
            MigrationConflict.conflict_type == CONFLICT_TYPE,
        )
    )


# ──────────────────────────────────────────────────────────────────────────
# Orchestration
# ──────────────────────────────────────────────────────────────────────────


def reconcile(
    session: Session,
    mapping: SubjectMapping,
    *,
    apply: bool = False,
) -> ReconcilePlan:
    """Run Track B reconciliation.

    Default (``apply=False``) is DRY-RUN: compute the plan, write NOTHING,
    return the plan for the operator to inspect. ``apply=True`` performs the
    backfill + re-resolve + conflict cleanup inside a single
    ``outbox_suppressed`` scope (one bulk_imported summary event) and the
    caller commits.
    """
    plan = build_plan(session, mapping)
    if not apply:
        return plan

    # Pre-load the contact lookup once for the apply pass.
    all_source_ids: list[str] = []
    for stat in plan.handles:
        all_source_ids.extend(stat.source_ids)
    contact_by_source = _load_contact_id_by_source_id(session, all_source_ids)

    counts: Counter[str] = Counter()

    async def _drive() -> None:
        async with outbox_suppressed(
            "dt_reconcile:track_b_assignments",
            session,  # type: ignore[arg-type]
            metadata=plan.summary(),
        ):
            seen_users: set[str] = set()
            for stat in plan.handles:
                mapped = mapping.get(stat.handle)
                if mapped is not None and mapped.is_service:
                    # Service account: delete every conflict for this handle,
                    # create NO assignment and set NO subject.
                    for sid in stat.source_ids:
                        _delete_conflict(session, source_id=sid)
                        counts["service_cleared"] += 1
                    continue
                if mapped is None or stat.wp_user_id is None:
                    continue
                if stat.wp_user_id not in seen_users:
                    _upsert_staff_subject(
                        session, wp_user_id=stat.wp_user_id, mapped=mapped
                    )
                    counts["staff_link"] += 1
                    seen_users.add(stat.wp_user_id)
                    if mapped.link_auth_identity and mapped.email:
                        _upsert_identity_link(session, mapped=mapped)
                        counts["identity_link"] += 1
                for sid in stat.source_ids:
                    contact_id = contact_by_source.get(sid)
                    if contact_id is None:
                        continue
                    if _resolve_assignment(
                        session, contact_id=contact_id, subject_id=mapped.subject_id
                    ):
                        counts["assignment"] += 1
                    # Whether the assignment was newly placed or already
                    # owned by this subject, the no_subject condition is now
                    # satisfied — clear the conflict.
                    _delete_conflict(session, source_id=sid)
                    counts["conflict_cleared"] += 1
            # NOTE: no per-batch event is emitted here. Anything written inside
            # outbox_suppressed is swallowed (counted, never persisted), so the
            # bulk_imported summary that outbox_suppressed itself emits is the
            # only event downstream consumers see for this run.

    asyncio.run(_drive())
    session.commit()

    plan.applied = True
    plan.staff_links_to_set = counts["staff_link"]
    plan.identity_links_to_set = counts["identity_link"]
    plan.assignments_to_resolve = counts["assignment"]
    plan.conflicts_to_clear = counts["conflict_cleared"]
    plan.service_handles_cleared = counts["service_cleared"]
    plan.assignments_discarded = counts["service_cleared"]
    return plan


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────


def _format_report(plan: ReconcilePlan, *, mapping: SubjectMapping) -> str:
    lines: list[str] = []
    mode = "APPLY" if plan.applied else "DRY-RUN (no writes)"
    lines.append(f"== Track B: assignee_no_subject reconciliation [{mode}] ==")
    lines.append(
        f"open conflicts: {plan.total_conflicts}  "
        f"distinct handles: {len(plan.handles)}"
    )
    lines.append("")
    lines.append("distinct assigned_to handles (most frequent first):")
    for stat in plan.handles:
        mapped = mapping.get(stat.handle)
        if mapped is None:
            target = "— UNMAPPED —"
        elif mapped.is_service:
            target = "— SERVICE (clear, no assignment) —"
        else:
            target = mapped.subject_id
        lines.append(
            f"  {stat.handle:<14} count={stat.count:<4} "
            f"wp_user_id={stat.wp_user_id or '?':<6} -> {target}"
        )
    lines.append("")
    s = plan.summary()
    verb = "set" if plan.applied else "would set"
    lines.append(f"staff_identity_link b2c_subject {verb}: {s['staff_links_to_set']}")
    lines.append(
        f"identity_link (auth) {verb}: {s['identity_links_to_set']}"
    )
    lines.append(
        f"assignments {'resolved' if plan.applied else 'would resolve'}: "
        f"{s['assignments_to_resolve']}"
    )
    lines.append(
        f"conflicts {'cleared' if plan.applied else 'would clear'}: "
        f"{s['conflicts_to_clear']}"
    )
    lines.append(
        f"service-account conflicts {'cleared' if plan.applied else 'would clear'} "
        f"(no assignment): {s['service_handles_cleared']} "
        f"(assignments discarded: {s['assignments_discarded']})"
    )
    if plan.unmapped_handles:
        lines.append("")
        lines.append(
            "UNMAPPED handles (add to mapping config to clear): "
            + ", ".join(plan.unmapped_handles)
        )
    if plan.conflicts_unresolvable:
        lines.append("")
        lines.append(
            f"WARNING: {len(plan.conflicts_unresolvable)} conflict(s) reference a "
            "contact not present locally; left in place: "
            + ", ".join(plan.conflicts_unresolvable[:10])
            + (" ..." if len(plan.conflicts_unresolvable) > 10 else "")
        )
    if not plan.applied:
        lines.append("")
        lines.append("DRY-RUN: nothing was written. Re-run with --apply to commit.")
    return "\n".join(lines)


def _make_session(postgres_url: str) -> tuple[Engine, Session]:
    engine: Engine = create_engine(postgres_url, future=True)
    Session = sessionmaker(engine, expire_on_commit=False)
    return engine, Session()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="track_b_assignments",
        description="Reconcile DT 'assignee_no_subject' migration conflicts.",
    )
    p.add_argument(
        "--postgres-url",
        default=DEFAULT_POSTGRES_URL,
        help="SQLAlchemy psycopg2 URL for the core Postgres.",
    )
    p.add_argument(
        "--mapping",
        help="Path to a JSON mapping config {dt_handle: subject | {...}}. "
        "Optional for --diagnose.",
    )
    p.add_argument(
        "--diagnose",
        action="store_true",
        help="Only report distinct handles + counts; do not plan or write.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="WRITE the reconciliation. Without this flag the tool is a "
        "no-op dry-run (the default).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    engine, session = _make_session(args.postgres_url)
    try:
        if args.diagnose:
            stats = distinct_handles(session)
            total = sum(s.count for s in stats)
            print(
                f"== assignee_no_subject: {total} conflicts across "
                f"{len(stats)} distinct handles =="
            )
            for stat in stats:
                print(
                    f"  {stat.handle:<14} count={stat.count:<4} "
                    f"wp_user_id={stat.wp_user_id or '?'}"
                )
            return 0

        mapping = (
            SubjectMapping.from_file(args.mapping)
            if args.mapping
            else SubjectMapping(by_handle={})
        )
        plan = reconcile(session, mapping, apply=args.apply)
        print(_format_report(plan, mapping=mapping))
        return 0
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
