"""DT → Postgres ETL orchestrator (U9).

CLI entry point invoked via ``uv run --package jp-adopt-etl dt-etl``.
Drives the mappers + dt_source readers + pg_writer to perform a full or
delta load of DT data into the jp-adopt-core schema. All writes happen
inside :func:`jp_adopt_api.outbox_suppression.outbox_suppressed` so the
worker drain receives a single ``jp.adopt.v1.bulk_imported`` event per
ETL run instead of one event per imported row.

Tables imported (in dependency order):
  1. ``staff_identity_link``   (from wp_users)
  2. ``contacts``              (from wp_posts + wp_postmeta pivot; + contact_profile)
  3. ``contact_assignment``    (from assigned_to postmeta → B2C subject)
  4. ``activity_log``          (from wp_comments + wp_dt_activity_log history)
  5. ``adopter_interest``      (from each contact's fpg_submission_data JSON)

Each table writes one ``etl_run`` row at start, increments counters as
rows are processed, and finalizes ``ended_at`` at the end (or after an
exception, with ``errors`` incremented).

Note on sync vs async: the API service is asyncpg; this is psycopg2.
The two stacks share ORM model definitions but never share a session.
The outbox_suppressed context manager is async — we run it via
``asyncio.run`` inside ``run_etl`` so the existing primitive still owns
the suppression bookkeeping.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from jp_adopt_api.models import (
    ActivityLog,
    AdopterInterest,
    Contact,
    ContactAssignment,
    ContactProfile,
    EtlDeletedInSource,
    EtlRun,
    Fpg,
    MigrationConflict,
    Outbox,
    StaffIdentityLink,
)
from jp_adopt_api.outbox_suppression import outbox_suppressed
from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from jp_adopt_etl.dt_source import (
    DEFAULT_BATCH_SIZE,
    fetch_max_modified,
    iter_activity_log,
    iter_comments,
    iter_contacts,
    iter_users,
    load_postmeta,
    open_engine,
)
from jp_adopt_etl.mappers.activity_history import map_activity_log_row
from jp_adopt_etl.mappers.assignment import parse_assigned_user_id
from jp_adopt_etl.mappers.comments import map_comment
from jp_adopt_etl.mappers.contacts import map_contact, pivot_postmeta
from jp_adopt_etl.mappers.interests import parse_fpg_submission_data
from jp_adopt_etl.mappers.profile import map_contact_profile
from jp_adopt_etl.mappers.status import Mode, UnmappedStatusError
from jp_adopt_etl.mappers.users import map_user

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Dry-run snapshot dataclasses
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class _EtlRunSnapshot:
    """In-memory copy of an EtlRun row plus the original id so dry-run can
    re-insert with a fresh id and remap FK references on dependent rows."""

    original_id: uuid.UUID
    table_name: str
    mode: str
    started_at: datetime
    ended_at: datetime | None
    watermark_from: datetime | None
    source_max_modified_at: datetime | None
    rows_in: int
    rows_out_inserted: int
    rows_out_updated: int
    rows_out_skipped: int
    rows_in_conflict: int
    errors: int

    @classmethod
    def from_row(cls, row: EtlRun) -> _EtlRunSnapshot:
        return cls(
            original_id=row.id,
            table_name=row.table_name,
            mode=row.mode,
            started_at=row.started_at,
            ended_at=row.ended_at,
            watermark_from=row.watermark_from,
            source_max_modified_at=row.source_max_modified_at,
            rows_in=row.rows_in,
            rows_out_inserted=row.rows_out_inserted,
            rows_out_updated=row.rows_out_updated,
            rows_out_skipped=row.rows_out_skipped,
            rows_in_conflict=row.rows_in_conflict,
            errors=row.errors,
        )

    def to_kwargs(self) -> dict[str, Any]:
        return {
            "table_name": self.table_name,
            "mode": self.mode,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "watermark_from": self.watermark_from,
            "source_max_modified_at": self.source_max_modified_at,
            "rows_in": self.rows_in,
            "rows_out_inserted": self.rows_out_inserted,
            "rows_out_updated": self.rows_out_updated,
            "rows_out_skipped": self.rows_out_skipped,
            "rows_in_conflict": self.rows_in_conflict,
            "errors": self.errors,
        }


@dataclass
class _DeletedInSourceSnapshot:
    original_etl_run_id: uuid.UUID
    table_name: str
    source_system: str
    source_id: str
    last_seen_at: datetime | None
    detected_at: datetime

    def to_kwargs(self, new_etl_run_id: uuid.UUID) -> dict[str, Any]:
        return {
            "etl_run_id": new_etl_run_id,
            "table_name": self.table_name,
            "source_system": self.source_system,
            "source_id": self.source_id,
            "last_seen_at": self.last_seen_at,
            "detected_at": self.detected_at,
        }


@dataclass
class _DryRunCapture:
    pre_conflict_ids: set[uuid.UUID] = field(default_factory=set)
    pre_deleted_ids: set[uuid.UUID] = field(default_factory=set)
    pre_outbox_bulk_imported_ids: set[uuid.UUID] = field(default_factory=set)
    etl_runs: list[_EtlRunSnapshot] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────
# etl_run lifecycle helpers
# ──────────────────────────────────────────────────────────────────────────


@contextmanager
def etl_run(
    session: Session,
    *,
    table_name: str,
    mode: Mode,
    watermark_from: datetime | None,
):
    """Open an etl_run row for the duration of one table's import. The
    yielded row is updated in-place; the context manager commits the
    final timestamp + counters on exit (including the error path).
    """
    row = EtlRun(
        id=uuid.uuid4(),
        table_name=table_name,
        mode=mode,
        started_at=datetime.now(UTC),
        watermark_from=watermark_from,
        rows_in=0,
        rows_out_inserted=0,
        rows_out_updated=0,
        rows_out_skipped=0,
        rows_in_conflict=0,
        errors=0,
    )
    session.add(row)
    session.flush()
    try:
        yield row
    except Exception:
        row.errors += 1
        raise
    finally:
        row.ended_at = datetime.now(UTC)
        session.flush()


def _record_conflict(
    session: Session,
    *,
    source_system: str,
    source_id: str,
    table_name: str,
    conflict_type: str,
    source_value: dict[str, Any] | None = None,
    local_value: dict[str, Any] | None = None,
) -> None:
    """Insert a migration_conflicts row, deduped by natural key.

    Migration 0025 adds a partial unique index on
    ``(source_system, source_id, table_name, conflict_type)`` so an hourly
    cron that re-detects the same conflict each run does not unbounded-grow
    the table.
    """
    session.execute(
        pg_insert(MigrationConflict)
        .values(
            id=uuid.uuid4(),
            source_system=source_system,
            source_id=source_id,
            table_name=table_name,
            conflict_type=conflict_type,
            source_value=source_value,
            local_value=local_value,
        )
        .on_conflict_do_nothing(
            index_elements=[
                "source_system",
                "source_id",
                "table_name",
                "conflict_type",
            ],
            index_where=text("source_id IS NOT NULL"),
        )
    )


# ──────────────────────────────────────────────────────────────────────────
# Per-table import functions
# ──────────────────────────────────────────────────────────────────────────


def import_users(
    *,
    mysql_conn: Connection,
    pg_session: Session,
    mode: Mode,
) -> dict[str, int]:
    """Import wp_users → staff_identity_link. Idempotent on dt_user_id."""
    counts = {
        "rows_in": 0,
        "rows_out_inserted": 0,
        "rows_out_updated": 0,
        "rows_out_skipped": 0,
    }
    for user_row in iter_users(mysql_conn):
        counts["rows_in"] += 1
        kwargs = map_user(user_row)
        if not kwargs["email"]:
            # wp_users without an email never authored anything actionable;
            # skip rather than insert a row that can't resolve.
            counts["rows_out_skipped"] += 1
            continue
        stmt = (
            pg_insert(StaffIdentityLink)
            .values(id=uuid.uuid4(), **kwargs)
            .on_conflict_do_update(
                index_elements=["dt_user_id"],
                set_={
                    "email": kwargs["email"],
                    "email_normalized": kwargs["email_normalized"],
                    "display_name": kwargs["display_name"],
                    "status": kwargs["status"],
                },
            )
            .returning(StaffIdentityLink.id, StaffIdentityLink.linked_at)
        )
        result = pg_session.execute(stmt).one()
        # Crude insert-vs-update discrimination: linked_at < ~1s ago = insert
        if (datetime.now(UTC) - result.linked_at).total_seconds() < 1:
            counts["rows_out_inserted"] += 1
        else:
            counts["rows_out_updated"] += 1
        del result
    return counts


def _load_existing_dt_user_id_to_link(pg_session: Session) -> dict[str, uuid.UUID]:
    rows = pg_session.execute(
        select(StaffIdentityLink.dt_user_id, StaffIdentityLink.id)
    ).all()
    return {row.dt_user_id: row.id for row in rows}


def _load_existing_dt_post_id_to_contact(pg_session: Session) -> dict[str, uuid.UUID]:
    rows = pg_session.execute(
        select(Contact.source_id, Contact.id).where(Contact.source_system == "dt")
    ).all()
    return {row.source_id: row.id for row in rows if row.source_id is not None}


def _load_email_owners(
    pg_session: Session,
) -> dict[str, tuple[str | None, str | None]]:
    """Map ``email_normalized`` → the (source_system, source_id) that owns it.
    Used to honor the partial unique index on contacts.email_normalized: DT
    permits duplicate emails across contacts, the new system does not."""
    rows = pg_session.execute(
        select(
            Contact.email_normalized, Contact.source_system, Contact.source_id
        ).where(Contact.email_normalized.is_not(None))
    ).all()
    return {r.email_normalized: (r.source_system, r.source_id) for r in rows}


def import_contacts(
    *,
    mysql_conn: Connection,
    pg_session: Session,
    mode: Mode,
    watermark: datetime | None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, int]:
    """Import wp_posts(post_type='contacts') + wp_postmeta → contacts.

    Re-runs use ``(source_system='dt', source_id)`` as the conflict key
    and skip rows where ``local_modified_after_import = true``.
    """
    counts = {
        "rows_in": 0,
        "rows_out_inserted": 0,
        "rows_out_updated": 0,
        "rows_out_skipped": 0,
        "rows_in_conflict": 0,
    }
    email_owner = _load_email_owners(pg_session)
    batch: list[dict[str, Any]] = []
    for post in iter_contacts(mysql_conn, watermark=watermark, batch_size=batch_size):
        batch.append(post)
        if len(batch) >= batch_size:
            _flush_contact_batch(
                mysql_conn, pg_session, batch, mode, counts, email_owner
            )
            batch.clear()
    if batch:
        _flush_contact_batch(mysql_conn, pg_session, batch, mode, counts, email_owner)
    return counts


def _flush_contact_batch(
    mysql_conn: Connection,
    pg_session: Session,
    batch: Iterable[dict[str, Any]],
    mode: Mode,
    counts: dict[str, int],
    email_owner: dict[str, tuple[str | None, str | None]],
) -> None:
    batch_list = list(batch)
    post_ids = [row["ID"] for row in batch_list]
    meta_by_post = load_postmeta(mysql_conn, post_ids)
    for post_row in batch_list:
        counts["rows_in"] += 1
        source_id = str(post_row["ID"])
        meta_rows = meta_by_post.get(post_row["ID"], [])
        try:
            kwargs = map_contact(post_row=post_row, meta_rows=meta_rows, mode=mode)
        except UnmappedStatusError as e:
            counts["rows_in_conflict"] += 1
            _record_conflict(
                pg_session,
                source_system="dt",
                source_id=source_id,
                table_name="contacts",
                conflict_type=f"unmapped_status:{e.party_kind}",
                source_value={"raw_status": e.source_value},
            )
            if mode == "dry_run":
                raise
            counts["rows_out_skipped"] += 1
            continue
        # Honor the partial unique index on email_normalized. DT permits the
        # same email on multiple contacts; the new system does not. Keep the
        # contact but drop the colliding email and flag it for review.
        email = kwargs.get("email_normalized")
        if email:
            owner = email_owner.get(email)
            if owner is not None and owner != ("dt", source_id):
                counts["rows_in_conflict"] += 1
                _record_conflict(
                    pg_session,
                    source_system="dt",
                    source_id=source_id,
                    table_name="contacts",
                    conflict_type="duplicate_email",
                    source_value={"email_normalized": email},
                )
                kwargs["email_normalized"] = None
            else:
                email_owner[email] = ("dt", source_id)
        # ON CONFLICT (source_system, source_id) DO UPDATE … WHERE
        # local_modified_after_import = false. WHERE is on the EXCLUDED
        # row's match against the existing row's flag — Postgres lets us
        # condition the DO UPDATE on the existing row state via the
        # ``where=`` clause on the on_conflict_do_update.
        stmt = (
            pg_insert(Contact)
            .values(id=uuid.uuid4(), **kwargs)
            .on_conflict_do_update(
                # Partial unique index from migration 0009 — must repeat
                # its WHERE predicate so Postgres targets the right index.
                index_elements=["source_system", "source_id"],
                index_where=text(
                    "source_system IS NOT NULL AND source_id IS NOT NULL"
                ),
                set_={
                    "party_kind": kwargs["party_kind"],
                    "display_name": kwargs["display_name"],
                    "adopter_status": kwargs.get("adopter_status"),
                    "facilitator_status": kwargs.get("facilitator_status"),
                    "email_normalized": kwargs.get("email_normalized"),
                    "phone": kwargs.get("phone"),
                    "origin": kwargs.get("origin"),
                },
                where=Contact.local_modified_after_import.is_(False),
            )
            .returning(Contact.id, Contact.local_modified_after_import)
        )
        result = pg_session.execute(stmt).one_or_none()
        if result is None:
            # The row exists AND local_modified_after_import=true — skip
            # and record a conflict so Amy can review what's diverged.
            counts["rows_out_skipped"] += 1
            counts["rows_in_conflict"] += 1
            _record_conflict(
                pg_session,
                source_system="dt",
                source_id=source_id,
                table_name="contacts",
                conflict_type="local_modified_after_import",
                source_value={"display_name": kwargs.get("display_name")},
            )
            continue
        # Heuristic: if the row was just inserted, the FK index hasn't been
        # touched before. We don't get a discriminator from ON CONFLICT DO
        # UPDATE … RETURNING; treat all returns as inserts-or-updates and
        # rely on rows_in vs total contacts for the audit. The plan calls
        # for "rows_out" counts; we expose rows_out_inserted + _updated as
        # the rows that got written through (whether new or refreshed).
        counts["rows_out_inserted"] += 1

        # Populate the 1:1 contact_profile with the JP-custom adoption fields.
        profile = map_contact_profile(pivot_postmeta(meta_rows))
        if profile is not None:
            pg_session.execute(
                pg_insert(ContactProfile)
                .values(id=uuid.uuid4(), contact_id=result.id, **profile)
                .on_conflict_do_update(
                    index_elements=["contact_id"],
                    set_=profile,
                )
            )
        del result


def sweep_deleted_contacts(
    mysql_conn: Connection,
    pg_session: Session,
    etl_run_id: uuid.UUID,
) -> int:
    """Record DT contacts that were imported previously but are absent from
    the current source snapshot. Never hard-deletes — writes to
    ``etl_deleted_in_source`` for Amy to review. Full-run only (the caller
    skips this on watermarked delta runs). Idempotent: a source_id already
    recorded is not duplicated.
    """
    seen = {
        str(post["ID"]) for post in iter_contacts(mysql_conn, watermark=None)
    }
    existing = {
        sid
        for (sid,) in pg_session.execute(
            select(Contact.source_id).where(Contact.source_system == "dt")
        ).all()
        if sid is not None
    }
    already = {
        sid
        for (sid,) in pg_session.execute(
            select(EtlDeletedInSource.source_id).where(
                EtlDeletedInSource.source_system == "dt",
                EtlDeletedInSource.table_name == "contacts",
            )
        ).all()
    }
    recorded = 0
    for source_id in existing - seen - already:
        pg_session.add(
            EtlDeletedInSource(
                id=uuid.uuid4(),
                etl_run_id=etl_run_id,
                table_name="contacts",
                source_system="dt",
                source_id=source_id,
            )
        )
        recorded += 1
    return recorded


def import_comments(
    *,
    mysql_conn: Connection,
    pg_session: Session,
    mode: Mode,
    watermark: datetime | None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, int]:
    """Import wp_comments → activity_log. Resolves author via
    staff_identity_link; missing wp_users → legacy_unknown."""
    counts = {
        "rows_in": 0,
        "rows_out_inserted": 0,
        "rows_out_updated": 0,
        "rows_out_skipped": 0,
    }
    post_to_contact = _load_existing_dt_post_id_to_contact(pg_session)
    user_to_link = _load_existing_dt_user_id_to_link(pg_session)
    for comment_row in iter_comments(
        mysql_conn, watermark=watermark, batch_size=batch_size
    ):
        counts["rows_in"] += 1
        source_post_id = str(comment_row.get("comment_post_ID"))
        contact_id = post_to_contact.get(source_post_id)
        if contact_id is None:
            # Comment without a corresponding imported Contact (parent
            # post not migrated). Skip — re-runs will pick it up once
            # the contact lands.
            counts["rows_out_skipped"] += 1
            continue
        user_id = comment_row.get("user_id")
        author_link_id = (
            user_to_link.get(str(user_id))
            if user_id and int(user_id) != 0
            else None
        )
        kwargs = map_comment(
            comment_row=comment_row,
            contact_id=contact_id,
            author_link_id=author_link_id,
        )
        stmt = (
            pg_insert(ActivityLog)
            .values(id=uuid.uuid4(), **kwargs)
            .on_conflict_do_nothing(
                index_elements=["source_system", "source_id"],
                index_where=text("source_id IS NOT NULL"),
            )
            .returning(ActivityLog.id)
        )
        result = pg_session.execute(stmt).one_or_none()
        if result is not None:
            counts["rows_out_inserted"] += 1
        else:
            counts["rows_out_skipped"] += 1

    _resolve_activity_threading(pg_session)
    return counts


def _resolve_activity_threading(pg_session: Session) -> None:
    """Second pass: link reply comments to their parent. The first pass
    stashes the source parent id in ``source_metadata.parent_source_id``;
    now that every comment is imported, resolve it to the parent's new UUID.
    """
    pg_session.execute(
        text(
            "UPDATE activity_log AS child "
            "SET parent_id = parent.id "
            "FROM activity_log AS parent "
            "WHERE child.source_system = 'dt' "
            "AND parent.source_system = 'dt' "
            "AND child.parent_id IS NULL "
            "AND parent.source_id = child.source_metadata ->> 'parent_source_id'"
        )
    )


def import_activity_history(
    *,
    mysql_conn: Connection,
    pg_session: Session,
    mode: Mode,
    watermark: datetime | None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, int]:
    """Import wp_dt_activity_log field-change rows → activity_log
    (kind='field_change'). Idempotent on ``source_id='histlog:<histid>'``."""
    counts = {"rows_in": 0, "rows_out_inserted": 0, "rows_out_skipped": 0}
    obj_to_contact = _load_existing_dt_post_id_to_contact(pg_session)
    user_to_link = _load_existing_dt_user_id_to_link(pg_session)
    for row in iter_activity_log(
        mysql_conn, watermark=watermark, batch_size=batch_size
    ):
        counts["rows_in"] += 1
        contact_id = obj_to_contact.get(str(row.get("object_id")))
        if contact_id is None:
            counts["rows_out_skipped"] += 1
            continue
        user_id = row.get("user_id")
        author_link_id = (
            user_to_link.get(str(user_id))
            if user_id and int(user_id) != 0
            else None
        )
        kwargs = map_activity_log_row(
            row=row, contact_id=contact_id, author_link_id=author_link_id
        )
        stmt = (
            pg_insert(ActivityLog)
            .values(id=uuid.uuid4(), **kwargs)
            .on_conflict_do_nothing(
                index_elements=["source_system", "source_id"],
                index_where=text("source_id IS NOT NULL"),
            )
            .returning(ActivityLog.id)
        )
        if pg_session.execute(stmt).one_or_none() is not None:
            counts["rows_out_inserted"] += 1
        else:
            counts["rows_out_skipped"] += 1
    return counts


def _min_watermark(*values: datetime | None) -> datetime | None:
    """Return the *earliest* of the given watermarks, normalizing naive
    datetimes to UTC so MySQL-naive and epoch-derived values compare.

    The activity_log target merges two source streams (wp_comments +
    wp_dt_activity_log) into a single watermark column. Using MAX would
    skip rows from the lagging stream on the next delta run — e.g. a
    back-dated comment whose date < histlog_max could never be picked up.
    MIN re-reads a slice of the leading stream on the next run, but the
    ON CONFLICT upsert is idempotent so the only cost is repeat work.

    Edge case: if one stream is empty (max returns None for that source)
    on the first run, the only timestamp comes from the other stream.
    A subsequent back-dated row inserted into the empty stream with a
    date earlier than that timestamp would be missed. Mitigation in
    practice: install-time full scan (--mode production without
    --watermark) before flipping the hourly cron on. Long-term fix:
    per-source watermark columns (deferred to a follow-up migration).
    """
    normalized = [
        (v if v.tzinfo else v.replace(tzinfo=UTC)) for v in values if v is not None
    ]
    return min(normalized, default=None)


def _load_existing_fpg_ids(pg_session: Session) -> set[str]:
    rows = pg_session.execute(select(Fpg.people_id3)).all()
    return {str(r.people_id3) for r in rows if r.people_id3 is not None}


def import_interests(
    *,
    mysql_conn: Connection,
    pg_session: Session,
    mode: Mode,
    watermark: datetime | None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, int]:
    """Import per-FPG interests from each contact's ``fpg_submission_data``
    postmeta (JSON) → ``adopter_interest``.

    Idempotent on ``(source_system='dt', source_id='<post_id>:<people_id3>')``.
    Interests whose ``people_id3`` is absent from ``fpg`` are skipped and
    recorded as conflicts (the FK would otherwise abort the batch); operators
    run ``sync_fpg`` before cutover so this should be rare.
    """
    counts = {"rows_in": 0, "rows_out_inserted": 0, "rows_out_skipped": 0}
    post_to_contact = _load_existing_dt_post_id_to_contact(pg_session)
    fpg_ids = _load_existing_fpg_ids(pg_session)

    batch: list[dict[str, Any]] = []
    for post in iter_contacts(mysql_conn, watermark=watermark, batch_size=batch_size):
        batch.append(post)
        if len(batch) >= batch_size:
            _flush_interest_batch(
                mysql_conn, pg_session, batch, post_to_contact, fpg_ids, counts
            )
            batch.clear()
    if batch:
        _flush_interest_batch(
            mysql_conn, pg_session, batch, post_to_contact, fpg_ids, counts
        )
    return counts


def _flush_interest_batch(
    mysql_conn: Connection,
    pg_session: Session,
    batch: Iterable[dict[str, Any]],
    post_to_contact: dict[str, uuid.UUID],
    fpg_ids: set[str],
    counts: dict[str, int],
) -> None:
    batch_list = list(batch)
    meta_by_post = load_postmeta(mysql_conn, [row["ID"] for row in batch_list])
    for post_row in batch_list:
        post_id = str(post_row["ID"])
        contact_id = post_to_contact.get(post_id)
        if contact_id is None:
            continue
        meta = pivot_postmeta(meta_by_post.get(post_row["ID"], []))
        for interest in parse_fpg_submission_data(meta.get("fpg_submission_data")):
            counts["rows_in"] += 1
            people_id3 = interest["people_id3"]
            if people_id3 not in fpg_ids:
                counts["rows_out_skipped"] += 1
                _record_conflict(
                    pg_session,
                    source_system="dt",
                    source_id=f"{post_id}:{people_id3}",
                    table_name="adopter_interest",
                    conflict_type="fpg_not_found",
                    source_value={"people_id3": people_id3},
                )
                continue
            stmt = (
                pg_insert(AdopterInterest)
                .values(
                    id=uuid.uuid4(),
                    contact_id=contact_id,
                    source_system="dt",
                    source_id=f"{post_id}:{people_id3}",
                    **interest,
                )
                .on_conflict_do_update(
                    index_elements=["source_system", "source_id"],
                    index_where=text("source_id IS NOT NULL"),
                    set_={
                        "engagement_status": interest["engagement_status"],
                        "facilitation_services": interest["facilitation_services"],
                        "network_services": interest["network_services"],
                        "commitment_types": interest["commitment_types"],
                    },
                )
            )
            pg_session.execute(stmt)
            counts["rows_out_inserted"] += 1


def _load_dt_user_id_to_subject(pg_session: Session) -> dict[str, str]:
    rows = pg_session.execute(
        select(StaffIdentityLink.dt_user_id, StaffIdentityLink.b2c_subject_id).where(
            StaffIdentityLink.b2c_subject_id.is_not(None)
        )
    ).all()
    return {r.dt_user_id: r.b2c_subject_id for r in rows}


def import_assignment(
    *,
    mysql_conn: Connection,
    pg_session: Session,
    mode: Mode,
    watermark: datetime | None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, int]:
    """Import DT ``assigned_to`` → contact_assignment (1:1 owner).

    The wp_user_id resolves to a B2C subject via ``staff_identity_link``.
    Staff who have not yet signed in have a NULL ``b2c_subject_id``, so their
    assignments are skipped and recorded as conflicts; a re-run picks them up
    once they sign in. ``wp_dt_share`` sub-assignments are out of scope
    (contact_assignment is 1:1)."""
    counts = {"rows_in": 0, "rows_out_inserted": 0, "rows_out_skipped": 0}
    post_to_contact = _load_existing_dt_post_id_to_contact(pg_session)
    user_to_subject = _load_dt_user_id_to_subject(pg_session)

    batch: list[dict[str, Any]] = []
    for post in iter_contacts(mysql_conn, watermark=watermark, batch_size=batch_size):
        batch.append(post)
        if len(batch) >= batch_size:
            _flush_assignment_batch(
                mysql_conn, pg_session, batch, post_to_contact, user_to_subject, counts
            )
            batch.clear()
    if batch:
        _flush_assignment_batch(
            mysql_conn, pg_session, batch, post_to_contact, user_to_subject, counts
        )
    return counts


def _flush_assignment_batch(
    mysql_conn: Connection,
    pg_session: Session,
    batch: Iterable[dict[str, Any]],
    post_to_contact: dict[str, uuid.UUID],
    user_to_subject: dict[str, str],
    counts: dict[str, int],
) -> None:
    batch_list = list(batch)
    meta_by_post = load_postmeta(mysql_conn, [row["ID"] for row in batch_list])
    for post_row in batch_list:
        post_id = str(post_row["ID"])
        contact_id = post_to_contact.get(post_id)
        if contact_id is None:
            continue
        meta = pivot_postmeta(meta_by_post.get(post_row["ID"], []))
        wp_user_id = parse_assigned_user_id(meta.get("assigned_to"))
        if wp_user_id is None:
            continue
        counts["rows_in"] += 1
        subject = user_to_subject.get(wp_user_id)
        if subject is None:
            counts["rows_out_skipped"] += 1
            _record_conflict(
                pg_session,
                source_system="dt",
                source_id=post_id,
                table_name="contact_assignment",
                conflict_type="assignee_no_subject",
                source_value={"assigned_to": meta.get("assigned_to")},
            )
            continue
        # ON CONFLICT only updates rows previously placed by the ETL — a
        # staff reassignment in jp-adopt-core (assigned_by != 'dt_import')
        # is protected against being clobbered by an hourly delta re-run.
        stmt = (
            pg_insert(ContactAssignment)
            .values(
                contact_id=contact_id,
                user_subject_id=subject,
                assigned_by="dt_import",
            )
            .on_conflict_do_update(
                index_elements=["contact_id"],
                set_={"user_subject_id": subject},
                where=ContactAssignment.assigned_by == "dt_import",
            )
            .returning(ContactAssignment.contact_id)
        )
        result = pg_session.execute(stmt).one_or_none()
        if result is None:
            counts["rows_out_skipped"] += 1
            _record_conflict(
                pg_session,
                source_system="dt",
                source_id=post_id,
                table_name="contact_assignment",
                conflict_type="local_assignment_override",
                source_value={"dt_assigned_to": meta.get("assigned_to")},
            )
        else:
            counts["rows_out_inserted"] += 1


# ──────────────────────────────────────────────────────────────────────────
# Dry-run replay
# ──────────────────────────────────────────────────────────────────────────


def _capture_dry_run_pre_state(pg_session: Session) -> _DryRunCapture:
    """Snapshot the audit-table row ids that already exist so a later
    diff (post-flush, pre-rollback) identifies what THIS run added."""
    return _DryRunCapture(
        pre_conflict_ids=set(
            pg_session.execute(select(MigrationConflict.id)).scalars().all()
        ),
        pre_deleted_ids=set(
            pg_session.execute(select(EtlDeletedInSource.id)).scalars().all()
        ),
        pre_outbox_bulk_imported_ids=set(
            pg_session.execute(
                select(Outbox.id).where(
                    Outbox.event_type == "jp.adopt.v1.bulk_imported"
                )
            )
            .scalars()
            .all()
        ),
    )


def _replay_dry_run_audit(
    pg_session: Session, capture: _DryRunCapture
) -> None:
    """Discard every data write from the in-flight transaction, then
    re-add the audit rows (etl_run + migration_conflicts +
    etl_deleted_in_source + bulk_imported Outbox) with fresh ids so a
    rehearsal still surfaces exactly what would happen in production.

    Safe to call on the exception path: the per-table work that failed
    leaves its partial writes inside the same transaction; rollback
    discards them, and the snapshots captured so far are still replayed.
    """
    try:
        pg_session.flush()
    except Exception:
        # If flush itself raises (e.g. deferred constraint), the session
        # is in an aborted state. Rollback restores it; nothing to replay.
        pg_session.rollback()
        return

    new_conflicts = [
        {
            "source_system": c.source_system,
            "source_id": c.source_id,
            "table_name": c.table_name,
            "conflict_type": c.conflict_type,
            "source_value": c.source_value,
            "local_value": c.local_value,
            "detected_at": c.detected_at,
        }
        for c in pg_session.execute(select(MigrationConflict)).scalars().all()
        if c.id not in capture.pre_conflict_ids
    ]
    new_deleted = [
        _DeletedInSourceSnapshot(
            original_etl_run_id=d.etl_run_id,
            table_name=d.table_name,
            source_system=d.source_system,
            source_id=d.source_id,
            last_seen_at=d.last_seen_at,
            detected_at=d.detected_at,
        )
        for d in pg_session.execute(select(EtlDeletedInSource)).scalars().all()
        if d.id not in capture.pre_deleted_ids
    ]
    new_bulk_imported = [
        {
            "event_type": o.event_type,
            "payload_json": o.payload_json,
            "created_at": o.created_at,
        }
        for o in pg_session.execute(
            select(Outbox).where(
                Outbox.event_type == "jp.adopt.v1.bulk_imported"
            )
        )
        .scalars()
        .all()
        if o.id not in capture.pre_outbox_bulk_imported_ids
    ]

    pg_session.rollback()

    etl_run_id_remap: dict[uuid.UUID, uuid.UUID] = {}
    for snap in capture.etl_runs:
        new_id = uuid.uuid4()
        etl_run_id_remap[snap.original_id] = new_id
        pg_session.add(EtlRun(id=new_id, **snap.to_kwargs()))
    for conflict_kwargs in new_conflicts:
        pg_session.add(MigrationConflict(id=uuid.uuid4(), **conflict_kwargs))
    for deleted_snap in new_deleted:
        # Skip if the parent etl_run snapshot was never captured (e.g.
        # sweep_deleted_contacts wrote a row but the parent table's
        # context manager raised before snapshot append). A KeyError here
        # would mask the real exception in the exception-path branch.
        new_run_id = etl_run_id_remap.get(deleted_snap.original_etl_run_id)
        if new_run_id is None:
            continue
        pg_session.add(
            EtlDeletedInSource(
                id=uuid.uuid4(),
                **deleted_snap.to_kwargs(new_run_id),
            )
        )
    for outbox_kwargs in new_bulk_imported:
        pg_session.add(Outbox(id=uuid.uuid4(), **outbox_kwargs))


# ──────────────────────────────────────────────────────────────────────────
# Watermark resolution (for the hourly cron)
# ──────────────────────────────────────────────────────────────────────────


def resolve_auto_watermark(postgres_url: str) -> datetime | None:
    """Return the next-run watermark inferred from prior etl_run rows.

    Strategy: for each watermarked table, find the latest successful
    ``source_max_modified_at`` (errors=0, mode='production'), then take the
    MIN across tables. The MIN is the conservative choice — re-reads some
    rows on the next run for the leading table, but never skips rows in a
    lagging one. The ON CONFLICT upserts are idempotent so the only cost
    of re-reading is wall-clock.

    Returns ``None`` when no prior successful production run exists, so
    the caller falls back to a full scan. Designed for `--watermark auto`
    in the scheduled cron.
    """
    engine = create_engine(postgres_url, future=True)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT MIN(latest_per_table) AS watermark
                    FROM (
                      SELECT table_name,
                             MAX(source_max_modified_at) AS latest_per_table
                      FROM etl_run
                      WHERE mode = 'production'
                        AND errors = 0
                        AND source_max_modified_at IS NOT NULL
                      GROUP BY table_name
                    ) t
                    """
                )
            ).one_or_none()
    finally:
        engine.dispose()
    if row is None or row.watermark is None:
        return None
    wm: datetime = row.watermark
    return wm if wm.tzinfo else wm.replace(tzinfo=UTC)


# ──────────────────────────────────────────────────────────────────────────
# Public runner
# ──────────────────────────────────────────────────────────────────────────


def run_etl(
    *,
    mysql_url: str,
    postgres_url: str,
    tables: list[str],
    mode: Mode,
    watermark: datetime | None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, dict[str, int]]:
    """Run the ETL against the two databases. Returns a dict keyed by
    table_name with per-table counts. The function blocks until the
    entire import is done.
    """
    mysql_engine: Engine = open_engine(mysql_url)
    pg_engine: Engine = create_engine(postgres_url, future=True)
    SessionLocal = sessionmaker(pg_engine, expire_on_commit=False, autoflush=False)

    async def _drive() -> dict[str, dict[str, int]]:
        results: dict[str, dict[str, int]] = {}
        with SessionLocal() as pg_session, mysql_engine.connect() as mysql_conn:
            capture = (
                _capture_dry_run_pre_state(pg_session)
                if mode == "dry_run"
                else _DryRunCapture()
            )
            try:
                async with outbox_suppressed(
                    f"dt_etl:{','.join(tables)}",
                    pg_session,  # type: ignore[arg-type]  # async-safe context var; session is sync
                    metadata={
                        "mode": mode,
                        "mysql_url_scheme": mysql_url.split("://", 1)[0],
                        "tables": tables,
                        "started_at": datetime.now(UTC).isoformat(),
                    },
                ) as ctx:
                    for table_name in tables:
                        with etl_run(
                            pg_session,
                            table_name=table_name,
                            mode=mode,
                            watermark_from=watermark,
                        ) as run_row:
                            if table_name == "staff_identity_link":
                                counts = import_users(
                                    mysql_conn=mysql_conn,
                                    pg_session=pg_session,
                                    mode=mode,
                                )
                            elif table_name == "contacts":
                                counts = import_contacts(
                                    mysql_conn=mysql_conn,
                                    pg_session=pg_session,
                                    mode=mode,
                                    watermark=watermark,
                                    batch_size=batch_size,
                                )
                                run_row.source_max_modified_at = fetch_max_modified(
                                    mysql_conn, table="wp_posts"
                                )
                                # Full-run only: flag contacts that vanished
                                # from source since a prior import.
                                if watermark is None:
                                    sweep_deleted_contacts(
                                        mysql_conn, pg_session, run_row.id
                                    )
                            elif table_name == "activity_log":
                                counts = import_comments(
                                    mysql_conn=mysql_conn,
                                    pg_session=pg_session,
                                    mode=mode,
                                    watermark=watermark,
                                    batch_size=batch_size,
                                )
                                history = import_activity_history(
                                    mysql_conn=mysql_conn,
                                    pg_session=pg_session,
                                    mode=mode,
                                    watermark=watermark,
                                    batch_size=batch_size,
                                )
                                for key, value in history.items():
                                    counts[key] = counts.get(key, 0) + value
                                run_row.source_max_modified_at = _min_watermark(
                                    fetch_max_modified(
                                        mysql_conn, table="wp_comments"
                                    ),
                                    fetch_max_modified(
                                        mysql_conn, table="wp_dt_activity_log"
                                    ),
                                )
                            elif table_name == "adopter_interest":
                                counts = import_interests(
                                    mysql_conn=mysql_conn,
                                    pg_session=pg_session,
                                    mode=mode,
                                    watermark=watermark,
                                    batch_size=batch_size,
                                )
                            elif table_name == "contact_assignment":
                                counts = import_assignment(
                                    mysql_conn=mysql_conn,
                                    pg_session=pg_session,
                                    mode=mode,
                                    watermark=watermark,
                                    batch_size=batch_size,
                                )
                            else:
                                raise ValueError(f"unknown table {table_name!r}")
                            run_row.rows_in = counts.get("rows_in", 0)
                            run_row.rows_out_inserted = counts.get(
                                "rows_out_inserted", 0
                            )
                            run_row.rows_out_updated = counts.get(
                                "rows_out_updated", 0
                            )
                            run_row.rows_out_skipped = counts.get(
                                "rows_out_skipped", 0
                            )
                            run_row.rows_in_conflict = counts.get(
                                "rows_in_conflict", 0
                            )
                            results[table_name] = counts
                        # Snapshot the audit row immediately so an exception
                        # in a later table still leaves a replayable record
                        # of the tables that completed.
                        capture.etl_runs.append(
                            _EtlRunSnapshot.from_row(run_row)
                        )
                        # In production mode, commit after each table so a
                        # crash in a later table preserves the audit trail
                        # AND the data this table already wrote. dry_run
                        # defers persistence to the snapshot-and-replay
                        # block below so data writes can still be rolled
                        # back at the end.
                        if mode == "production":
                            pg_session.commit()
                    ctx.metadata["finished_at"] = datetime.now(UTC).isoformat()
            except Exception:
                # On exception in dry_run, replay the audit trail captured so
                # far so the operator can triage whatever tables completed.
                # Production-mode exceptions: per-table commits above mean
                # tables that completed already have their audit + data
                # persisted; the failing table's partial writes were rolled
                # back implicitly when SessionLocal exits on exception.
                if mode == "dry_run":
                    try:
                        _replay_dry_run_audit(pg_session, capture)
                        pg_session.commit()
                    except Exception:
                        logger.exception(
                            "dry-run audit replay failed on exception path"
                        )
                        pg_session.rollback()
                raise
            if mode == "dry_run":
                _replay_dry_run_audit(pg_session, capture)
            pg_session.commit()
        mysql_engine.dispose()
        pg_engine.dispose()
        return results

    return asyncio.run(_drive())


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dt-etl",
        description="DT MySQL → jp-adopt-core Postgres batch ETL (U9).",
    )
    parser.add_argument(
        "--mysql-url",
        required=True,
        help="SQLAlchemy URL for the DT MySQL source (mysql+pymysql://...)",
    )
    parser.add_argument(
        "--postgres-url",
        required=True,
        help="SQLAlchemy URL for the jp-adopt-core Postgres target (postgresql+psycopg2://...)",
    )
    parser.add_argument(
        "--table",
        action="append",
        choices=[
            "all",
            "staff_identity_link",
            "contacts",
            "contact_assignment",
            "activity_log",
            "adopter_interest",
        ],
        help=(
            "Which target table(s) to import. Pass multiple times or use 'all' "
            "for the full sequence."
        ),
        default=None,
    )
    parser.add_argument(
        "--mode",
        choices=["dry_run", "production"],
        default="dry_run",
        help=(
            "dry_run fails loudly on unmapped values; production maps to "
            "'unknown' and records to migration_conflicts."
        ),
    )
    parser.add_argument(
        "--watermark",
        type=str,
        default=None,
        help=(
            "ISO 8601 timestamp or the literal 'auto'. With 'auto', the "
            "orchestrator queries etl_run and computes MIN(MAX("
            "source_max_modified_at)) per table across prior successful "
            "production runs. Designed for the hourly cron."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Rows per fetch batch (wp_postmeta lookup chunk size).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Set the root logger to DEBUG.",
    )
    return parser.parse_args(argv)


def _resolve_tables(table_args: list[str] | None) -> list[str]:
    if not table_args or "all" in table_args:
        # Dependency order: identity-links before activity_log (author FK);
        # contacts before activity_log (contact FK) and adopter_interest.
        return [
            "staff_identity_link",
            "contacts",
            "contact_assignment",
            "activity_log",
            "adopter_interest",
        ]
    seen: set[str] = set()
    ordered: list[str] = []
    for name in [
            "staff_identity_link",
            "contacts",
            "contact_assignment",
            "activity_log",
            "adopter_interest",
        ]:
        if name in table_args and name not in seen:
            ordered.append(name)
            seen.add(name)
    return ordered


def _resolve_cli_watermark(
    raw: str | None, postgres_url: str
) -> datetime | None:
    """Translate the --watermark CLI string into a concrete timestamp.

    ``None`` → no filter (full scan).
    ``"auto"`` → look up the prior run's high-water mark from etl_run.
    Otherwise: parse as ISO 8601, anchored to UTC.

    Raises :class:`SystemExit` with exit code 2 on malformed input, to
    match argparse's behavior. The CLI used to put ISO parsing inside
    ``type=lambda`` so argparse would catch invalid values at parse
    time; that moved here when ``--watermark auto`` was added, so we
    have to mirror argparse's error contract by hand.
    """
    if raw is None:
        return None
    if raw.strip().lower() == "auto":
        wm = resolve_auto_watermark(postgres_url)
        if wm is None:
            logger.info(
                "--watermark auto: falling back to full scan (no "
                "watermark available from prior successful production "
                "runs — either etl_run is empty or all qualifying rows "
                "have NULL source_max_modified_at)"
            )
        else:
            logger.info("--watermark auto resolved to %s", wm.isoformat())
        return wm
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=UTC)
    except ValueError as e:
        logger.error(
            "Invalid --watermark value %r: must be ISO 8601 or 'auto' (%s)",
            raw,
            e,
        )
        raise SystemExit(2) from e


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    tables = _resolve_tables(args.table)
    watermark = _resolve_cli_watermark(args.watermark, args.postgres_url)
    logger.info(
        "Starting ETL: tables=%s mode=%s watermark=%s",
        tables,
        args.mode,
        watermark.isoformat() if watermark else None,
    )
    try:
        results = run_etl(
            mysql_url=args.mysql_url,
            postgres_url=args.postgres_url,
            tables=tables,
            mode=args.mode,
            watermark=watermark,
            batch_size=args.batch_size,
        )
    except UnmappedStatusError as e:
        logger.error("Unmapped status: %s", e)
        return 2
    for table_name, counts in results.items():
        logger.info("%s: %s", table_name, counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "etl_run",
    "import_activity_history",
    "import_assignment",
    "import_comments",
    "import_contacts",
    "import_interests",
    "import_users",
    "main",
    "resolve_auto_watermark",
    "run_etl",
    "sweep_deleted_contacts",
]
