"""Track C — reconcile ``fpg_not_found`` migration conflicts.

The main ETL skips an ``adopter_interest`` row whenever its ``people_id3``
is absent from the ``fpg`` reference table (the FK would otherwise abort the
whole batch) and records a ``fpg_not_found`` MigrationConflict
(``source_id='<post_id>:<people_id3>'``, ``source_value={'people_id3': ...}``,
``table_name='adopter_interest'``). In prod there are 12 such rows.

Most of those are stale only because ``fpg`` hadn't been refreshed from the
forms export yet. This track:

  1. Refreshes ``fpg`` via the existing ``sync_fpg`` path (the same
     forms-export GET + ``upsert_fpg`` ON CONFLICT used at cutover).
  2. Re-processes each ``fpg_not_found`` conflict whose ``people_id3`` now
     exists in ``fpg``: re-reads the DT contact by ``post_id`` from the DT
     MySQL source, parses its ``fpg_submission_data``, upserts the matching
     ``AdopterInterest`` (idempotent on ``(source_system, source_id)``), and
     DELETES the now-resolved conflict by its natural key.
  3. Reports any ``people_id3`` STILL absent after the refresh as a
     genuinely-stale DT reference for manual triage (left in place).

DRY-RUN BY DEFAULT. ``mode='dry_run'`` (the default; CLI without ``--apply``)
makes zero net DB change: the fpg refresh, interest upserts, and conflict
deletes all run inside one transaction that is rolled back, and only the
audit trail (an ``etl_run`` row + a would-resolve report) is surfaced.
``mode='production'`` (``--apply``) commits. All writes are idempotent so a
dry-run-then-apply, or a re-apply, is safe.

DT MySQL is credential-gated (1Password) and unavailable in many
environments; tests mock the DT readers and never open a live connection.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from jp_adopt_api.models import AdopterInterest, Fpg, MigrationConflict
from jp_adopt_api.outbox_suppression import outbox_suppressed
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from jp_adopt_etl.dt_source import fetch_contact, load_postmeta, open_engine
from jp_adopt_etl.mappers.contacts import pivot_postmeta
from jp_adopt_etl.mappers.interests import parse_fpg_submission_data

logger = logging.getLogger(__name__)

CONFLICT_SOURCE_SYSTEM = "dt"
CONFLICT_TABLE_NAME = "adopter_interest"
CONFLICT_TYPE = "fpg_not_found"

# Literal modes, mirroring orchestrator's Mode (dry_run is non-mutating).
Mode = str  # 'dry_run' | 'production'


class FpgRefreshUnavailable(RuntimeError):
    """Raised by ``_refresh_fpg`` when the forms-export config is missing or
    unreachable, so the fpg reference table could NOT be refreshed.

    The driver catches this and marks the report ``fpg_refresh_skipped`` —
    the remaining conflicts are reported as 'staleness unknown' rather than
    being mislabeled 'genuinely stale' (we never refreshed the source of
    truth, so we cannot prove they are stale)."""


# ──────────────────────────────────────────────────────────────────────────
# Result types
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class FpgConflict:
    """One ``fpg_not_found`` conflict row, parsed into its parts."""

    source_id: str  # "<post_id>:<people_id3>"
    post_id: str
    people_id3: str

    @classmethod
    def from_row(
        cls, source_id: str, source_value: dict[str, Any] | None
    ) -> FpgConflict:
        # source_id is the authoritative "<post_id>:<people_id3>"; source_value
        # carries people_id3 redundantly. Split on the LAST ':' so a post_id
        # never collides with a people_id3 that contains no colon (it never does).
        post_id, _, pid_from_sid = source_id.rpartition(":")
        people_id3 = ""
        if isinstance(source_value, dict):
            people_id3 = str(source_value.get("people_id3") or "").strip()
        if not people_id3:
            people_id3 = pid_from_sid
        if not post_id:
            # No ':' in source_id — degrade gracefully (shouldn't happen in prod).
            post_id = source_id
        return cls(source_id=source_id, post_id=post_id, people_id3=people_id3)


@dataclass
class ReconcileReport:
    mode: Mode
    fpg_rows_refreshed: int = 0
    # True when the fpg refresh was SKIPPED (forms-export config missing /
    # unreachable). In that case ``still_stale`` is NOT proven stale — the
    # reference table simply wasn't refreshed, so staleness is UNKNOWN.
    fpg_refresh_skipped: bool = False
    conflicts_seen: int = 0
    # Conflicts whose people_id3 now exists in fpg AND whose DT contact still
    # carries the interest → AdopterInterest upserted + conflict deleted.
    resolved: list[FpgConflict] = field(default_factory=list)
    # Conflicts whose people_id3 is STILL absent from fpg after refresh →
    # genuinely-stale DT reference, left in place for manual triage.
    still_stale: list[FpgConflict] = field(default_factory=list)
    # people_id3 now in fpg, but the DT contact no longer lists that interest
    # (e.g. the operator edited the DT submission). Conflict left in place.
    not_in_source: list[FpgConflict] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "fpg_rows_refreshed": self.fpg_rows_refreshed,
            "fpg_refresh_skipped": self.fpg_refresh_skipped,
            "conflicts_seen": self.conflicts_seen,
            "resolved": [c.source_id for c in self.resolved],
            # Key these under the meaning that matches whether the refresh ran:
            # proven-stale only when the fpg table was actually refreshed.
            (
                "staleness_unknown"
                if self.fpg_refresh_skipped
                else "still_stale"
            ): sorted({c.people_id3 for c in self.still_stale}),
            "not_in_source": [c.source_id for c in self.not_in_source],
        }


# ``fetch_contact`` (single-row DT re-read by post ID) is imported from the
# canonical ``jp_adopt_etl.dt_source`` module — Track A / dt_source already
# own that SELECT. Re-exported here so monkeypatch targets and the reprocess
# path reference one implementation instead of a divergent local copy.


# ──────────────────────────────────────────────────────────────────────────
# Conflict loading / fpg refresh
# ──────────────────────────────────────────────────────────────────────────


def load_fpg_not_found_conflicts(pg_session: Session) -> list[FpgConflict]:
    rows = pg_session.execute(
        select(MigrationConflict.source_id, MigrationConflict.source_value).where(
            MigrationConflict.source_system == CONFLICT_SOURCE_SYSTEM,
            MigrationConflict.table_name == CONFLICT_TABLE_NAME,
            MigrationConflict.conflict_type == CONFLICT_TYPE,
        )
    ).all()
    return [FpgConflict.from_row(r.source_id, r.source_value) for r in rows]


def _load_fpg_ids(pg_session: Session) -> set[str]:
    rows = pg_session.execute(select(Fpg.people_id3)).all()
    return {str(r.people_id3) for r in rows if r.people_id3 is not None}


async def _refresh_fpg(pg_session: Session) -> int:
    """Refresh the ``fpg`` table from the forms export via the existing
    ``sync_fpg`` code path, but WITHOUT committing (the caller owns the
    transaction so dry-run can roll the whole thing back).

    Returns the number of fpg rows upserted. Raises
    :class:`FpgRefreshUnavailable` when the forms-export settings are unset so
    the caller can mark the run 'staleness unknown' instead of mislabeling
    unresolved conflicts as 'genuinely stale' — we cannot prove staleness
    without refreshing the reference table.
    """
    # Imported lazily so importing this module never triggers settings/config
    # loading (keeps pure-unit mapper tests free of API config requirements).
    from jp_adopt_api.config import get_settings
    from jp_adopt_api.scripts.sync_fpg import fetch_from_forms_export, normalize_rows

    settings = get_settings()
    export_url = (settings.forms_export_url or "").strip()
    api_key = (settings.forms_export_api_key or "").strip()
    if not export_url or not api_key:
        raise FpgRefreshUnavailable(
            "FORMS_EXPORT_URL / FORMS_EXPORT_API_KEY unset; cannot refresh the "
            "fpg reference table. Unresolved conflicts cannot be proven stale "
            "without a refresh — set the forms-export config and re-run."
        )

    raw = await fetch_from_forms_export(export_url, api_key)
    rows = normalize_rows(raw)
    return _upsert_fpg_no_commit(pg_session, rows)


def _upsert_fpg_no_commit(
    pg_session: Session, rows: list[dict[str, Any]], *, chunk: int = 500
) -> int:
    """Idempotent ``fpg`` upsert (ON CONFLICT on the people_id3 PK), mirroring
    ``sync_fpg.upsert_fpg`` but on the SYNC session and WITHOUT committing."""
    if not rows:
        return 0
    written = 0
    for start in range(0, len(rows), chunk):
        batch = rows[start : start + chunk]
        stmt = pg_insert(Fpg).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=["people_id3"],
            set_={
                "name": stmt.excluded.name,
                "country_code": stmt.excluded.country_code,
                "frontier": stmt.excluded.frontier,
            },
        )
        pg_session.execute(stmt)
        written += len(batch)
    return written


# ──────────────────────────────────────────────────────────────────────────
# Re-process one conflict
# ──────────────────────────────────────────────────────────────────────────


def _load_post_id_to_contact(pg_session: Session) -> dict[str, uuid.UUID]:
    from jp_adopt_api.models import Contact

    rows = pg_session.execute(
        select(Contact.id, Contact.source_id).where(
            Contact.source_system == CONFLICT_SOURCE_SYSTEM,
            Contact.source_id.is_not(None),
        )
    ).all()
    return {str(r.source_id): r.id for r in rows}


def _reprocess_conflict(
    *,
    mysql_conn: Connection,
    pg_session: Session,
    conflict: FpgConflict,
    contact_id: uuid.UUID,
) -> bool:
    """Re-read the DT contact for ``conflict``, upsert the matching interest,
    and DELETE the conflict row. Returns True if resolved, False if the DT
    contact no longer lists that interest (conflict left in place)."""
    post_row = fetch_contact(mysql_conn, conflict.post_id)
    if post_row is None:
        return False
    meta_rows = load_postmeta(mysql_conn, [post_row["ID"]]).get(post_row["ID"], [])
    meta = pivot_postmeta(meta_rows)
    interests = parse_fpg_submission_data(meta.get("fpg_submission_data"))
    match = next(
        (i for i in interests if str(i["people_id3"]) == conflict.people_id3), None
    )
    if match is None:
        # The DT submission no longer carries this people_id3; nothing to import.
        return False

    stmt = (
        pg_insert(AdopterInterest)
        .values(
            id=uuid.uuid4(),
            contact_id=contact_id,
            source_system=CONFLICT_SOURCE_SYSTEM,
            source_id=conflict.source_id,
            **match,
        )
        .on_conflict_do_update(
            index_elements=["source_system", "source_id"],
            index_where=text("source_id IS NOT NULL"),
            set_={
                "engagement_status": match["engagement_status"],
                "facilitation_services": match["facilitation_services"],
                "network_services": match["network_services"],
                "commitment_types": match["commitment_types"],
            },
        )
    )
    pg_session.execute(stmt)
    _delete_conflict(pg_session, conflict)
    return True


def _delete_conflict(pg_session: Session, conflict: FpgConflict) -> None:
    """Resolve = DELETE the conflict by its natural key (there is no
    resolved/status column on MigrationConflict). Idempotent: a later ETL run
    only re-creates it if the underlying condition still holds."""
    pg_session.execute(
        delete(MigrationConflict).where(
            MigrationConflict.source_system == CONFLICT_SOURCE_SYSTEM,
            MigrationConflict.source_id == conflict.source_id,
            MigrationConflict.table_name == CONFLICT_TABLE_NAME,
            MigrationConflict.conflict_type == CONFLICT_TYPE,
        )
    )


# ──────────────────────────────────────────────────────────────────────────
# etl_run audit row (reuse the shared model directly; no orchestrator edit)
# ──────────────────────────────────────────────────────────────────────────


def _write_etl_run(pg_session: Session, report: ReconcileReport) -> None:
    from jp_adopt_api.models import EtlRun

    pg_session.add(
        EtlRun(
            id=uuid.uuid4(),
            table_name="reconcile:fpg_not_found",
            mode=report.mode,
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
            rows_in=report.conflicts_seen,
            rows_out_inserted=len(report.resolved),
            rows_out_updated=0,
            rows_out_skipped=len(report.still_stale) + len(report.not_in_source),
            rows_in_conflict=len(report.still_stale) + len(report.not_in_source),
            errors=0,
        )
    )


# ──────────────────────────────────────────────────────────────────────────
# Driver
# ──────────────────────────────────────────────────────────────────────────


def reconcile(
    *,
    mysql_conn: Connection,
    pg_session: Session,
    mode: Mode = "dry_run",
) -> ReconcileReport:
    """Core reconcile pass. Mutates ``pg_session`` but never commits — the
    caller (``run`` / a test) owns the transaction so dry-run can roll back.

    Steps: refresh fpg → re-process resolvable conflicts → classify the rest.
    """
    report = ReconcileReport(mode=mode)

    async def _do_refresh() -> int:
        # outbox_suppressed wraps ALL bulk writes (fpg upsert + interest
        # upserts + conflict deletes happen within this scope on the same
        # ContextVar-scoped session) into one jp.adopt.v1.bulk_imported summary
        # event instead of per-row Outbox rows. The sync session is passed with
        # a type: ignore exactly as the orchestrator does (suppression state is
        # a ContextVar, not tied to the async session).
        async with outbox_suppressed(
            "dt_reconcile:fpg_not_found",
            pg_session,  # type: ignore[arg-type]  # async-safe context var; session is sync
            metadata={"mode": mode, "track": "c_fpg_not_found"},
        ):
            try:
                report.fpg_rows_refreshed = await _refresh_fpg(pg_session)
            except FpgRefreshUnavailable:
                # Config missing/unreachable: proceed against whatever is
                # already in fpg, but FLAG that staleness is unknown so the
                # report never mislabels unresolved conflicts as proven-stale.
                logger.warning(
                    "fpg refresh skipped (forms-export config unavailable); "
                    "remaining conflicts reported as 'staleness unknown'."
                )
                report.fpg_refresh_skipped = True
            _reprocess_all(mysql_conn, pg_session, report)
        return report.fpg_rows_refreshed

    asyncio.run(_do_refresh())
    _write_etl_run(pg_session, report)
    return report


def _reprocess_all(
    mysql_conn: Connection, pg_session: Session, report: ReconcileReport
) -> None:
    conflicts = load_fpg_not_found_conflicts(pg_session)
    report.conflicts_seen = len(conflicts)
    fpg_ids = _load_fpg_ids(pg_session)
    post_to_contact = _load_post_id_to_contact(pg_session)

    for conflict in conflicts:
        if conflict.people_id3 not in fpg_ids:
            report.still_stale.append(conflict)
            continue
        contact_id = post_to_contact.get(conflict.post_id)
        if contact_id is None:
            # FPG exists now but we can't find the local contact to attach the
            # interest to — treat as not-resolvable here; leave the conflict.
            report.not_in_source.append(conflict)
            continue
        resolved = _reprocess_conflict(
            mysql_conn=mysql_conn,
            pg_session=pg_session,
            conflict=conflict,
            contact_id=contact_id,
        )
        if resolved:
            report.resolved.append(conflict)
        else:
            report.not_in_source.append(conflict)


def run(
    *,
    mysql_url: str,
    postgres_url: str,
    mode: Mode = "dry_run",
) -> ReconcileReport:
    """Open both DBs, run :func:`reconcile`, then commit (production) or roll
    back (dry_run). Mirrors the orchestrator's per-mode persistence shape so
    dry-run is provably non-mutating for data while still surfacing the audit
    ``etl_run`` row + the report.
    """
    mysql_engine: Engine = open_engine(mysql_url)
    pg_engine: Engine = create_engine(postgres_url, future=True)
    SessionLocal = sessionmaker(pg_engine, expire_on_commit=False, autoflush=False)
    try:
        with SessionLocal() as pg_session, mysql_engine.connect() as mysql_conn:
            try:
                report = reconcile(
                    mysql_conn=mysql_conn, pg_session=pg_session, mode=mode
                )
            except Exception:
                pg_session.rollback()
                raise
            if mode == "production":
                pg_session.commit()
            else:
                # Dry-run: discard every data write. The report is already a
                # plain in-memory object, so the operator still sees the
                # would-be effect after rollback.
                pg_session.rollback()
            return report
    finally:
        mysql_engine.dispose()
        pg_engine.dispose()


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="dt-reconcile-fpg",
        description=(
            "Reconcile fpg_not_found migration conflicts: refresh fpg, "
            "re-import now-resolvable adopter_interest rows, delete resolved "
            "conflicts, report genuinely-stale DT people-group references. "
            "DRY-RUN by default; pass --apply to write."
        ),
    )
    p.add_argument("--mysql-url", required=True, help="mysql+pymysql://… DT source")
    p.add_argument(
        "--postgres-url", required=True, help="postgresql+psycopg2://… core target"
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Write changes (mode=production). Omit for a non-mutating dry run.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)
    mode: Mode = "production" if args.apply else "dry_run"
    report = run(
        mysql_url=args.mysql_url, postgres_url=args.postgres_url, mode=mode
    )
    summary = report.as_dict()
    logger.info("track-c reconcile (%s): %s", mode, summary)
    if report.fpg_refresh_skipped:
        stale_label = (
            f"{len(report.still_stale)} interests with people_id3 of UNKNOWN "
            f"staleness — fpg refresh SKIPPED "
            f"({summary['staleness_unknown']})"
        )
    else:
        stale_label = (
            f"{len(report.still_stale)} interests with still-stale people_id3 "
            f"({summary['still_stale']})"
        )
    print(
        f"track-c fpg_not_found reconcile [{mode}]: "
        f"{len(report.resolved)} resolved, "
        f"{stale_label}, "
        f"{len(report.not_in_source)} no-longer-in-source; "
        f"fpg rows refreshed={report.fpg_rows_refreshed}."
    )
    if mode == "dry_run":
        print("DRY RUN — no changes were written. Re-run with --apply to commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
