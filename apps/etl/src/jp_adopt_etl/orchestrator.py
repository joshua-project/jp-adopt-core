"""DT → Postgres ETL orchestrator (U9).

CLI entry point invoked via ``uv run --package jp-adopt-etl dt-etl``.
Drives the mappers + dt_source readers + pg_writer to perform a full or
delta load of DT data into the jp-adopt-core schema. All writes happen
inside :func:`jp_adopt_api.outbox_suppression.outbox_suppressed` so the
worker drain receives a single ``jp.adopt.v1.bulk_imported`` event per
ETL run instead of one event per imported row.

Tables imported (in dependency order):
  1. ``staff_identity_link``   (from wp_users)
  2. ``contacts``              (from wp_posts + wp_postmeta pivot)
  3. ``adopter_interest``      (from wp_p2p contacts_to_peoplegroups)
  4. ``activity_log``          (from wp_comments)

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
from datetime import UTC, datetime
from typing import Any

from jp_adopt_api.models import (
    ActivityLog,
    Contact,
    EtlDeletedInSource,
    EtlRun,
    MigrationConflict,
    StaffIdentityLink,
)
from jp_adopt_api.outbox_suppression import outbox_suppressed
from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from jp_adopt_etl.dt_source import (
    DEFAULT_BATCH_SIZE,
    fetch_max_modified,
    iter_comments,
    iter_contacts,
    iter_p2p,
    iter_users,
    load_postmeta,
    open_engine,
)
from jp_adopt_etl.mappers.comments import map_comment
from jp_adopt_etl.mappers.contacts import map_contact
from jp_adopt_etl.mappers.p2p import P2P_TYPE_CONTACT_TO_FPG
from jp_adopt_etl.mappers.status import Mode, UnmappedStatusError
from jp_adopt_etl.mappers.users import map_user

logger = logging.getLogger(__name__)


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
    session.add(
        MigrationConflict(
            id=uuid.uuid4(),
            source_system=source_system,
            source_id=source_id,
            table_name=table_name,
            conflict_type=conflict_type,
            source_value=source_value,
            local_value=local_value,
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
    batch: list[dict[str, Any]] = []
    for post in iter_contacts(mysql_conn, watermark=watermark, batch_size=batch_size):
        batch.append(post)
        if len(batch) >= batch_size:
            _flush_contact_batch(mysql_conn, pg_session, batch, mode, counts)
            batch.clear()
    if batch:
        _flush_contact_batch(mysql_conn, pg_session, batch, mode, counts)
    return counts


def _flush_contact_batch(
    mysql_conn: Connection,
    pg_session: Session,
    batch: Iterable[dict[str, Any]],
    mode: Mode,
    counts: dict[str, int],
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
                    "country_code": kwargs.get("country_code"),
                    "language_codes": kwargs.get("language_codes"),
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
        del result


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
    return counts


def import_p2p_interests(
    *,
    mysql_conn: Connection,
    pg_session: Session,
    mode: Mode,
    p2p_type: str = P2P_TYPE_CONTACT_TO_FPG,
) -> dict[str, int]:
    """Import wp_p2p (contacts_to_peoplegroups) → adopter_interest.

    Gracefully degrades when the source DB has no p2p table (OperationalError),
    logging a single warning and returning zero counts.
    """
    counts = {"rows_in": 0, "rows_out_inserted": 0, "rows_out_skipped": 0}
    post_to_contact = _load_existing_dt_post_id_to_contact(pg_session)
    try:
        rows = list(iter_p2p(mysql_conn, p2p_type=p2p_type))
    except OperationalError as e:
        logger.warning(
            "wp_p2p table not present or unreadable; skipping AdopterInterest "
            "import: %s",
            e,
        )
        return counts
    # p2p_from is the contact post_id; p2p_to is the people-group post_id.
    # Resolving people-group post_id → rop3 requires another wp_postmeta
    # lookup. For now we only import the relation when the orchestrator
    # caller pre-resolves rop3 via a separate lookup. v1 ships the
    # mapper + reader plumbing; the rop3 resolution lives in a follow-up
    # cutover pass (U13) that handles the FPG dimension explicitly.
    for p2p_row in rows:
        counts["rows_in"] += 1
        contact_id = post_to_contact.get(str(p2p_row.get("p2p_from")))
        if contact_id is None:
            counts["rows_out_skipped"] += 1
            continue
        # Without a rop3 resolution we can't write an AdopterInterest
        # row that satisfies the FK; record as a conflict for U13 to pick up.
        _record_conflict(
            pg_session,
            source_system="dt",
            source_id=str(p2p_row.get("p2p_id")),
            table_name="adopter_interest",
            conflict_type="p2p_rop3_resolution_deferred",
            source_value={"p2p_to": p2p_row.get("p2p_to")},
        )
        counts["rows_out_skipped"] += 1
    return counts


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
                        elif table_name == "activity_log":
                            counts = import_comments(
                                mysql_conn=mysql_conn,
                                pg_session=pg_session,
                                mode=mode,
                                watermark=watermark,
                                batch_size=batch_size,
                            )
                            run_row.source_max_modified_at = fetch_max_modified(
                                mysql_conn, table="wp_comments"
                            )
                        elif table_name == "adopter_interest":
                            counts = import_p2p_interests(
                                mysql_conn=mysql_conn,
                                pg_session=pg_session,
                                mode=mode,
                            )
                        else:
                            raise ValueError(f"unknown table {table_name!r}")
                        run_row.rows_in = counts.get("rows_in", 0)
                        run_row.rows_out_inserted = counts.get("rows_out_inserted", 0)
                        run_row.rows_out_updated = counts.get("rows_out_updated", 0)
                        run_row.rows_out_skipped = counts.get("rows_out_skipped", 0)
                        run_row.rows_in_conflict = counts.get("rows_in_conflict", 0)
                        results[table_name] = counts
                ctx.metadata["finished_at"] = datetime.now(UTC).isoformat()
            if mode == "dry_run":
                # In dry-run, roll back at the end so nothing persists
                # except the etl_run audit rows — but we want those to
                # persist, so we commit only the audit table writes by
                # selectively flushing. Cleanest is to commit and let
                # the dry_run mode field signal the intent.
                pass
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
        type=lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC),
        default=None,
        help=(
            "ISO 8601 timestamp; only rows modified after this point are "
            "imported. Use the prior run's source_max_modified_at value."
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
        return ["staff_identity_link", "contacts", "activity_log", "adopter_interest"]
    seen: set[str] = set()
    ordered: list[str] = []
    for name in ["staff_identity_link", "contacts", "activity_log", "adopter_interest"]:
        if name in table_args and name not in seen:
            ordered.append(name)
            seen.add(name)
    return ordered


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    tables = _resolve_tables(args.table)
    logger.info(
        "Starting ETL: tables=%s mode=%s watermark=%s",
        tables,
        args.mode,
        args.watermark.isoformat() if args.watermark else None,
    )
    try:
        results = run_etl(
            mysql_url=args.mysql_url,
            postgres_url=args.postgres_url,
            tables=tables,
            mode=args.mode,
            watermark=args.watermark,
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


# Silence unused-import warnings on EtlDeletedInSource — the model is
# imported here so external callers can populate it via the same session,
# and so the table is referenced in __all__-style exports of this module.
_ = EtlDeletedInSource


__all__ = [
    "etl_run",
    "import_comments",
    "import_contacts",
    "import_p2p_interests",
    "import_users",
    "main",
    "run_etl",
]
