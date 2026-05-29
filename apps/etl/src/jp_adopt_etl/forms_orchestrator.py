"""Forms Postgres → adopt-core batch importer (issue #75).

CLI: ``uv run --package jp-adopt-etl forms-etl``

Reads jp-adopt-forms' ``adoption_submissions`` / ``facilitation_submissions``
tables, maps each row to the canonical intake payloads, and calls
:func:`jp_adopt_api.routers.intake.process_*_payload` inside
:func:`jp_adopt_api.outbox_suppression.outbox_suppressed` so drips/webhooks
see one ``jp.adopt.v1.bulk_imported`` event per run.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal

from jp_adopt_api.config import Settings, get_settings
from jp_adopt_api.models import Contact, EtlRun, MigrationConflict
from sqlalchemy import select
from jp_adopt_api.outbox_suppression import outbox_suppressed
from jp_adopt_api.routers.intake import (
    SOURCE_SYSTEM_FORMS,
    IntakeValidationError,
    process_adoption_payload,
    process_facilitation_payload,
)
from jp_adopt_api.schemas import AdoptionIntake, FacilitationIntake
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jp_adopt_etl.forms_source import (
    DEFAULT_BATCH_SIZE,
    fetch_max_created_at,
    iter_submissions,
    open_engine,
)
from jp_adopt_etl.mappers.forms import MapFailure, MapSuccess, map_submission_row

logger = logging.getLogger(__name__)

Mode = Literal["dry_run", "production"]
TABLE_NAME = "submissions"
SOURCE_SYSTEM = SOURCE_SYSTEM_FORMS


def _to_async_url(postgres_url: str) -> str:
    if "+asyncpg" in postgres_url:
        return postgres_url
    if "+psycopg2" in postgres_url:
        return postgres_url.replace("+psycopg2", "+asyncpg", 1)
    if postgres_url.startswith("postgresql://"):
        return postgres_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return postgres_url


@asynccontextmanager
async def etl_run(
    session: AsyncSession,
    *,
    table_name: str,
    mode: Mode,
    watermark_from: datetime | None,
):
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
    await session.flush()
    try:
        yield row
    finally:
        row.ended_at = datetime.now(UTC)
        await session.flush()


async def _already_imported(session: AsyncSession, source_id: str) -> bool:
    """True when this forms submission was imported in a prior run."""
    row = await session.execute(
        select(Contact.id)
        .where(
            Contact.source_system == SOURCE_SYSTEM,
            Contact.source_id == source_id,
        )
        .limit(1)
    )
    return row.scalar_one_or_none() is not None


async def _record_conflict(
    session: AsyncSession,
    *,
    source_id: str,
    conflict_type: str,
    source_value: dict[str, Any] | None = None,
) -> None:
    session.add(
        MigrationConflict(
            id=uuid.uuid4(),
            source_system=SOURCE_SYSTEM,
            source_id=source_id,
            table_name=TABLE_NAME,
            conflict_type=conflict_type,
            source_value=source_value,
        )
    )


async def _import_one(
    session: AsyncSession,
    *,
    mapped: MapSuccess,
    settings: Settings,
) -> str:
    """Returns outcome label: imported | blocked | skipped_conflict.

    The helper call runs inside ``session.begin_nested()`` (a SAVEPOINT) so
    any exception — including ``sqlalchemy.exc.IntegrityError`` from a
    concurrent insert or FK race — rolls back ONLY this row's writes. The
    outer transaction stays alive, and the follow-up ``_record_conflict``
    call writes its row against a clean session instead of inheriting the
    poisoned state that an IntegrityError leaves behind.
    """
    kwargs = {
        "override_created_at": mapped.created_at,
        "source_system": SOURCE_SYSTEM,
        "source_id": mapped.source_id,
    }
    try:
        async with session.begin_nested():
            if mapped.form_type == "adoption":
                assert isinstance(mapped.payload, AdoptionIntake)
                outcome = await process_adoption_payload(
                    session,
                    payload=mapped.payload,
                    settings=settings,
                    **kwargs,
                )
            else:
                assert isinstance(mapped.payload, FacilitationIntake)
                outcome = await process_facilitation_payload(
                    session,
                    payload=mapped.payload,
                    settings=settings,
                    **kwargs,
                )
    except IntakeValidationError as exc:
        await _record_conflict(
            session,
            source_id=mapped.source_id,
            conflict_type=exc.code if exc.message is None else f"{exc.code}: {exc.message}",
            source_value={"fields": exc.fields},
        )
        return "skipped_conflict"
    except Exception as exc:  # noqa: BLE001 — per-row defense in depth
        await _record_conflict(
            session,
            source_id=mapped.source_id,
            conflict_type=f"processing_error: {exc}",
            source_value={"form_type": mapped.form_type},
        )
        return "skipped_conflict"

    if outcome.was_blocked:
        return "blocked"
    return "imported"


async def _process_rows(
    session: AsyncSession,
    *,
    rows: Iterator[dict[str, Any]],
    settings: Settings,
    verbose: bool,
    commit_every: int | None = None,
) -> dict[str, int | datetime | None]:
    """Process all rows.

    When ``commit_every`` is set (production runs only), the orchestrator
    flushes pending writes to disk every N rows so a crash on row 9,500 of
    10,000 doesn't lose every prior row's progress, and so the outer
    transaction stays short enough to avoid blocking autovacuum. Dry-runs
    leave it ``None`` (the caller's outer ``begin_nested`` + rollback is the
    only correct shape there — any mid-run commit would defeat the dry-run).
    """
    counts: dict[str, int | datetime | None] = {
        "rows_in": 0,
        "imported": 0,
        "blocked": 0,
        "mapping_failed": 0,
        "skipped_conflict": 0,
        "skipped_already_imported": 0,
        "max_created_at": None,
    }
    rows_since_commit = 0
    for row in rows:
        counts["rows_in"] = int(counts["rows_in"]) + 1
        created_at = row.get("created_at")
        if isinstance(created_at, datetime):
            prev = counts["max_created_at"]
            if prev is None or created_at > prev:
                counts["max_created_at"] = created_at

        result = map_submission_row(row)
        if isinstance(result, MapFailure):
            counts["mapping_failed"] = int(counts["mapping_failed"]) + 1
            await _record_conflict(
                session,
                source_id=result.source_id or "unknown",
                conflict_type=result.reason,
                source_value=result.source_payload,
            )
            if verbose:
                logger.info(
                    "mapping_failed source_id=%s reason=%s",
                    result.source_id,
                    result.reason,
                )
            rows_since_commit += 1
        else:
            if await _already_imported(session, result.source_id):
                counts["skipped_already_imported"] = (
                    int(counts["skipped_already_imported"]) + 1
                )
                if verbose:
                    logger.info(
                        "skipped_already_imported form_type=%s source_id=%s",
                        result.form_type,
                        result.source_id,
                    )
                # No write happened; nothing to commit for this row.
            else:
                label = await _import_one(session, mapped=result, settings=settings)
                counts[label] = int(counts[label]) + 1
                if verbose:
                    logger.info(
                        "%s form_type=%s source_id=%s",
                        label,
                        result.form_type,
                        result.source_id,
                    )
                rows_since_commit += 1

        if commit_every is not None and rows_since_commit >= commit_every:
            await session.commit()
            rows_since_commit = 0
            if verbose:
                logger.info("batch_commit rows=%d", commit_every)

    return counts


async def run_forms_etl(
    *,
    forms_postgres_url: str,
    postgres_url: str,
    mode: Mode,
    watermark: datetime | None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    verbose: bool = False,
) -> dict[str, Any]:
    forms_engine = open_engine(forms_postgres_url)
    async_engine = create_async_engine(_to_async_url(postgres_url), future=True)
    SessionLocal = async_sessionmaker(async_engine, expire_on_commit=False)
    settings = get_settings()

    summary: dict[str, Any] = {}

    async with SessionLocal() as session:
        with forms_engine.connect() as forms_conn:
            rows = iter_submissions(
                forms_conn, watermark=watermark, batch_size=batch_size
            )

            async with etl_run(
                session,
                table_name=TABLE_NAME,
                mode=mode,
                watermark_from=watermark,
            ) as run_row:
                if mode == "dry_run":
                    async with session.begin_nested() as nested:
                        async with outbox_suppressed(
                            f"forms_etl:{TABLE_NAME}",
                            session,
                            metadata={
                                "mode": mode,
                                "source_system": SOURCE_SYSTEM,
                                "watermark": watermark.isoformat() if watermark else None,
                                "started_at": datetime.now(UTC).isoformat(),
                            },
                        ) as ctx:
                            counts = await _process_rows(
                                session,
                                rows=rows,
                                settings=settings,
                                verbose=verbose,
                            )
                            ctx.metadata["finished_at"] = datetime.now(UTC).isoformat()
                            ctx.metadata["counts"] = {
                                k: v
                                for k, v in counts.items()
                                if k != "max_created_at"
                            }
                        await nested.rollback()
                else:
                    async with outbox_suppressed(
                        f"forms_etl:{TABLE_NAME}",
                        session,
                        metadata={
                            "mode": mode,
                            "source_system": SOURCE_SYSTEM,
                            "watermark": watermark.isoformat() if watermark else None,
                            "started_at": datetime.now(UTC).isoformat(),
                        },
                    ) as ctx:
                        counts = await _process_rows(
                            session,
                            rows=rows,
                            settings=settings,
                            verbose=verbose,
                            # Production: flush every batch_size rows so a
                            # crash mid-run doesn't lose every prior row's
                            # work and the outer transaction stays short
                            # enough to avoid blocking autovacuum.
                            commit_every=batch_size,
                        )
                        ctx.metadata["finished_at"] = datetime.now(UTC).isoformat()
                        ctx.metadata["counts"] = {
                            k: v for k, v in counts.items() if k != "max_created_at"
                        }

                run_row.rows_in = int(counts["rows_in"])
                run_row.rows_out_inserted = int(counts["imported"])
                run_row.rows_out_skipped = (
                    int(counts["blocked"])
                    + int(counts["skipped_conflict"])
                    + int(counts["skipped_already_imported"])
                )
                run_row.rows_in_conflict = int(counts["mapping_failed"]) + int(
                    counts["skipped_conflict"]
                )
                run_row.errors = int(counts["mapping_failed"]) + int(
                    counts["skipped_conflict"]
                )

                max_seen = counts["max_created_at"]
                if isinstance(max_seen, datetime):
                    run_row.source_max_modified_at = max_seen
                elif mode != "dry_run":
                    run_row.source_max_modified_at = fetch_max_created_at(forms_conn)

                summary = {
                    "table": TABLE_NAME,
                    "mode": mode,
                    **{k: v for k, v in counts.items()},
                }

            await session.commit()

    forms_engine.dispose()
    await async_engine.dispose()
    return summary


def _parse_watermark(value: str) -> datetime:
    """Parse CLI watermark to UTC, preserving the instant for offset-aware input."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="forms-etl",
        description="jp-adopt-forms Postgres → adopt-core batch importer.",
    )
    parser.add_argument(
        "--forms-postgres-url",
        required=True,
        help="SQLAlchemy URL for jp-adopt-forms Postgres (postgresql+psycopg2://...)",
    )
    parser.add_argument(
        "--postgres-url",
        required=True,
        help="SQLAlchemy URL for adopt-core target Postgres",
    )
    parser.add_argument(
        "--mode",
        choices=["dry_run", "production"],
        default="dry_run",
        help="dry_run rolls back intake writes but persists etl_run audit row",
    )
    parser.add_argument(
        "--watermark",
        type=_parse_watermark,
        default=None,
        help="ISO 8601 timestamp; only rows with created_at > watermark are imported",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Fetch batch size for source reads",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Log per-row outcomes at INFO",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info(
        "Starting forms-etl mode=%s watermark=%s",
        args.mode,
        args.watermark.isoformat() if args.watermark else None,
    )
    summary = asyncio.run(
        run_forms_etl(
            forms_postgres_url=args.forms_postgres_url,
            postgres_url=args.postgres_url,
            mode=args.mode,
            watermark=args.watermark,
            batch_size=args.batch_size,
            verbose=args.verbose,
        )
    )
    logger.info("forms-etl summary: %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main", "run_forms_etl"]
