"""Integration tests for Track C (fpg_not_found reconcile) against a real
Postgres with the DT MySQL source fully mocked.

Mirrors the harness in ``test_orchestrator_integration.py``: real Postgres
at ETL_TEST_DATABASE_URL, all DT readers monkeypatched, and every test row
scoped to the 9xxx source_id range so the autouse cleanup can wipe it.

DT MySQL is NEVER connected — ``fetch_contact`` / ``load_postmeta`` /
``_refresh_fpg`` are monkeypatched. ``reconcile()`` is called directly with
a sentinel ``mysql_conn`` (the monkeypatched readers ignore it).
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
from jp_adopt_api.models import (
    AdopterInterest,
    Contact,
    EtlRun,
    Fpg,
    MigrationConflict,
)
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.orm import sessionmaker

from jp_adopt_etl.reconcile import track_c_fpg

ETL_TEST_DATABASE_URL = os.environ.get(
    "ETL_TEST_DATABASE_URL",
    "postgresql+psycopg2://jp_adopt:jp_adopt@127.0.0.1:5434/jp_adopt",
)

pytestmark = pytest.mark.skipif(
    "ETL_TEST_DATABASE_URL_DISABLE" in os.environ,
    reason="Postgres not available in this environment",
)

# Test data lives in the 9xxx source_id range + 9XX people_id3 range.
POST_ID = "9501"
PID_RESOLVABLE = "918421"   # will be added to fpg → resolvable
PID_STALE = "999999"        # never in fpg → genuinely-stale
SID_RESOLVABLE = f"{POST_ID}:{PID_RESOLVABLE}"
SID_STALE = f"{POST_ID}:{PID_STALE}"


@pytest.fixture
def pg_engine():
    engine = create_engine(ETL_TEST_DATABASE_URL, future=True)
    yield engine
    engine.dispose()


@pytest.fixture
def pg_session(pg_engine):
    Session = sessionmaker(pg_engine, expire_on_commit=False)
    with Session() as s:
        yield s


@pytest.fixture(autouse=True)
def _cleanup(pg_session):
    yield
    test_contact_ids = select(Contact.id).where(
        Contact.source_system == "dt", Contact.source_id.like("9%")
    )
    pg_session.execute(
        delete(AdopterInterest).where(
            AdopterInterest.source_system == "dt",
            AdopterInterest.source_id.like("9%"),
        )
    )
    pg_session.execute(
        delete(MigrationConflict).where(
            MigrationConflict.source_system == "dt",
            MigrationConflict.source_id.like("9%"),
        )
    )
    pg_session.execute(delete(Contact).where(Contact.id.in_(test_contact_ids)))
    pg_session.execute(delete(Fpg).where(Fpg.people_id3.like("9%")))
    pg_session.execute(
        text(
            "DELETE FROM etl_run WHERE table_name LIKE 'reconcile:%' "
            "AND started_at > now() - interval '5 minutes'"
        )
    )
    pg_session.commit()


def _seed_contact(pg_session) -> uuid.UUID:
    cid = uuid.uuid4()
    pg_session.add(
        Contact(
            id=cid,
            party_kind="adopter",
            display_name="Track C Test Adopter",
            source_system="dt",
            source_id=POST_ID,
        )
    )
    pg_session.flush()
    return cid


def _seed_conflicts(pg_session) -> None:
    for sid, pid in ((SID_RESOLVABLE, PID_RESOLVABLE), (SID_STALE, PID_STALE)):
        pg_session.add(
            MigrationConflict(
                id=uuid.uuid4(),
                source_system="dt",
                source_id=sid,
                table_name="adopter_interest",
                conflict_type="fpg_not_found",
                source_value={"people_id3": pid},
                local_value=None,
            )
        )
    pg_session.flush()


def _patch_dt(monkeypatch, *, interests_people_ids):
    """Mock the DT single-row re-read + postmeta load so no MySQL is touched."""

    def fake_fetch_contact(_conn, post_id):
        return {"ID": int(post_id)}

    fpg_submission = json.dumps(
        [
            {
                "peopleId3": pid,
                "engagementStatus": "ready",
                "facilitationServices": ["coaching"],
                "networkServices": [],
                "commitmentTypes": ["prayer"],
            }
            for pid in interests_people_ids
        ]
    )

    def fake_load_postmeta(_conn, post_ids):
        return {
            pid: [{"meta_key": "fpg_submission_data", "meta_value": fpg_submission}]
            for pid in post_ids
        }

    monkeypatch.setattr(track_c_fpg, "fetch_contact", fake_fetch_contact)
    monkeypatch.setattr(track_c_fpg, "load_postmeta", fake_load_postmeta)


def _patch_open_engine(monkeypatch, pg_engine):
    """``run()`` calls ``open_engine(mysql_url)`` then ``.connect()``. Point it
    at the Postgres sentinel engine — the mocked readers mean the MySQL
    connection is never actually queried (mirrors the orchestrator harness)."""
    monkeypatch.setattr(track_c_fpg, "open_engine", lambda _url: pg_engine)


def _patch_fpg_refresh(monkeypatch, *, add_resolvable: bool):
    """Replace the forms-export fpg refresh with a local upsert of the
    resolvable people_id3 (the stale one is intentionally never added)."""

    async def fake_refresh(session):
        if not add_resolvable:
            return 0
        return track_c_fpg._upsert_fpg_no_commit(
            session,
            [
                {
                    "people_id3": PID_RESOLVABLE,
                    "name": "Resolvable FPG",
                    "country_code": "US",
                    "frontier": True,
                }
            ],
        )

    monkeypatch.setattr(track_c_fpg, "_refresh_fpg", fake_refresh)


# ─── tests ──────────────────────────────────────────────────────────────────


def test_dry_run_is_non_mutating_but_reports(monkeypatch, pg_engine, pg_session):
    _seed_contact(pg_session)
    _seed_conflicts(pg_session)
    pg_session.commit()

    _patch_dt(monkeypatch, interests_people_ids=[PID_RESOLVABLE, PID_STALE])
    _patch_fpg_refresh(monkeypatch, add_resolvable=True)
    _patch_open_engine(monkeypatch, pg_engine)

    report = track_c_fpg.run(
        mysql_url="mysql+pymysql://sentinel",
        postgres_url=ETL_TEST_DATABASE_URL,
        mode="dry_run",
    )

    # Report shows the resolvable one would resolve, stale stays flagged.
    assert report.resolved and report.resolved[0].source_id == SID_RESOLVABLE
    assert report.still_stale and report.still_stale[0].people_id3 == PID_STALE

    # But DRY-RUN wrote nothing: conflicts both still present, no interest,
    # fpg not persisted, no etl_run row committed.
    remaining = pg_session.execute(
        select(MigrationConflict.source_id).where(
            MigrationConflict.source_system == "dt",
            MigrationConflict.source_id.like("9%"),
        )
    ).scalars().all()
    assert set(remaining) == {SID_RESOLVABLE, SID_STALE}

    interests = pg_session.execute(
        select(AdopterInterest).where(AdopterInterest.source_id == SID_RESOLVABLE)
    ).scalars().all()
    assert interests == []

    fpg = pg_session.execute(
        select(Fpg.people_id3).where(Fpg.people_id3 == PID_RESOLVABLE)
    ).scalars().all()
    assert fpg == []

    runs = pg_session.execute(
        select(EtlRun).where(
            EtlRun.table_name == "reconcile:fpg_not_found",
            EtlRun.started_at > text("now() - interval '5 minutes'"),
        )
    ).scalars().all()
    assert runs == []


def test_apply_resolves_resolvable_and_leaves_stale(
    monkeypatch, pg_engine, pg_session
):
    cid = _seed_contact(pg_session)
    _seed_conflicts(pg_session)
    pg_session.commit()

    _patch_dt(monkeypatch, interests_people_ids=[PID_RESOLVABLE, PID_STALE])
    _patch_fpg_refresh(monkeypatch, add_resolvable=True)
    _patch_open_engine(monkeypatch, pg_engine)

    report = track_c_fpg.run(
        mysql_url="mysql+pymysql://sentinel",
        postgres_url=ETL_TEST_DATABASE_URL,
        mode="production",
    )

    assert [c.source_id for c in report.resolved] == [SID_RESOLVABLE]
    assert [c.people_id3 for c in report.still_stale] == [PID_STALE]

    # The resolvable conflict is gone; the stale one remains for triage.
    remaining = pg_session.execute(
        select(MigrationConflict.source_id).where(
            MigrationConflict.source_system == "dt",
            MigrationConflict.source_id.like("9%"),
        )
    ).scalars().all()
    assert set(remaining) == {SID_STALE}

    # The AdopterInterest was upserted for the resolvable people_id3.
    interest = pg_session.execute(
        select(AdopterInterest).where(AdopterInterest.source_id == SID_RESOLVABLE)
    ).scalars().one()
    assert interest.contact_id == cid
    assert interest.people_id3 == PID_RESOLVABLE
    assert interest.engagement_status == "ready"

    # An etl_run audit row was committed.
    runs = pg_session.execute(
        select(EtlRun).where(EtlRun.table_name == "reconcile:fpg_not_found")
    ).scalars().all()
    assert len(runs) == 1
    assert runs[0].mode == "production"
    assert runs[0].rows_out_inserted == 1


def test_apply_is_idempotent(monkeypatch, pg_engine, pg_session):
    _seed_contact(pg_session)
    _seed_conflicts(pg_session)
    pg_session.commit()

    _patch_dt(monkeypatch, interests_people_ids=[PID_RESOLVABLE, PID_STALE])
    _patch_fpg_refresh(monkeypatch, add_resolvable=True)
    _patch_open_engine(monkeypatch, pg_engine)

    track_c_fpg.run(
        mysql_url="mysql+pymysql://sentinel",
        postgres_url=ETL_TEST_DATABASE_URL,
        mode="production",
    )
    # Second apply: resolvable conflict already gone; must not error or
    # duplicate the AdopterInterest row.
    report2 = track_c_fpg.run(
        mysql_url="mysql+pymysql://sentinel",
        postgres_url=ETL_TEST_DATABASE_URL,
        mode="production",
    )
    assert report2.resolved == []  # nothing left to resolve
    assert [c.people_id3 for c in report2.still_stale] == [PID_STALE]

    interests = pg_session.execute(
        select(AdopterInterest).where(AdopterInterest.source_id == SID_RESOLVABLE)
    ).scalars().all()
    assert len(interests) == 1


def test_not_in_source_when_dt_no_longer_lists_interest(
    monkeypatch, pg_engine, pg_session
):
    """people_id3 IS now in fpg AND the local contact exists, but the DT
    submission no longer carries that interest (operator edited DT) →
    ``_reprocess_conflict`` returns False → conflict goes to not_in_source and
    is LEFT in place. Exercises the second silent-skip branch."""
    _seed_contact(pg_session)
    _seed_conflicts(pg_session)
    pg_session.commit()

    # fpg now HAS the resolvable people_id3 …
    _patch_fpg_refresh(monkeypatch, add_resolvable=True)
    # … but the DT submission lists NEITHER interest (so the resolvable one is
    # no-longer-in-source rather than resolvable).
    _patch_dt(monkeypatch, interests_people_ids=[])
    _patch_open_engine(monkeypatch, pg_engine)

    report = track_c_fpg.run(
        mysql_url="mysql+pymysql://sentinel",
        postgres_url=ETL_TEST_DATABASE_URL,
        mode="production",
    )

    assert report.resolved == []
    # Resolvable pid was in fpg but absent from DT → not_in_source.
    assert SID_RESOLVABLE in [c.source_id for c in report.not_in_source]
    # Stale pid still flagged stale (never entered fpg).
    assert [c.people_id3 for c in report.still_stale] == [PID_STALE]

    # Both conflicts remain (neither resolved).
    remaining = pg_session.execute(
        select(MigrationConflict.source_id).where(
            MigrationConflict.source_system == "dt",
            MigrationConflict.source_id.like("9%"),
        )
    ).scalars().all()
    assert set(remaining) == {SID_RESOLVABLE, SID_STALE}


def test_not_in_source_when_local_contact_missing(
    monkeypatch, pg_engine, pg_session
):
    """people_id3 IS now in fpg, but NO local Contact exists for the post_id
    (so we can't attach the interest) → conflict goes to not_in_source and is
    left in place. Exercises the first silent-skip branch (contact_id None)."""
    # NOTE: deliberately do NOT seed the contact.
    _seed_conflicts(pg_session)
    pg_session.commit()

    _patch_fpg_refresh(monkeypatch, add_resolvable=True)
    _patch_dt(monkeypatch, interests_people_ids=[PID_RESOLVABLE, PID_STALE])
    _patch_open_engine(monkeypatch, pg_engine)

    report = track_c_fpg.run(
        mysql_url="mysql+pymysql://sentinel",
        postgres_url=ETL_TEST_DATABASE_URL,
        mode="production",
    )

    assert report.resolved == []
    # Resolvable pid in fpg but no local contact → not_in_source.
    assert SID_RESOLVABLE in [c.source_id for c in report.not_in_source]
    assert [c.people_id3 for c in report.still_stale] == [PID_STALE]

    # The resolvable conflict was NOT deleted (no contact to attach to).
    remaining = pg_session.execute(
        select(MigrationConflict.source_id).where(
            MigrationConflict.source_system == "dt",
            MigrationConflict.source_id.like("9%"),
        )
    ).scalars().all()
    assert SID_RESOLVABLE in set(remaining)


def test_all_stale_when_refresh_adds_nothing(monkeypatch, pg_engine, pg_session):
    _seed_contact(pg_session)
    _seed_conflicts(pg_session)
    pg_session.commit()

    _patch_dt(monkeypatch, interests_people_ids=[PID_RESOLVABLE, PID_STALE])
    _patch_fpg_refresh(monkeypatch, add_resolvable=False)
    _patch_open_engine(monkeypatch, pg_engine)

    report = track_c_fpg.run(
        mysql_url="mysql+pymysql://sentinel",
        postgres_url=ETL_TEST_DATABASE_URL,
        mode="production",
    )

    assert report.resolved == []
    assert {c.people_id3 for c in report.still_stale} == {PID_RESOLVABLE, PID_STALE}
    # Both conflicts remain.
    remaining = pg_session.execute(
        select(MigrationConflict.source_id).where(
            MigrationConflict.source_system == "dt",
            MigrationConflict.source_id.like("9%"),
        )
    ).scalars().all()
    assert set(remaining) == {SID_RESOLVABLE, SID_STALE}
