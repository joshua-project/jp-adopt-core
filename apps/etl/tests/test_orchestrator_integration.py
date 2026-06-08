"""U9 orchestrator integration tests against a real Postgres.

These tests monkey-patch the MySQL reader functions in ``dt_source`` so
the orchestrator can run end-to-end without a live MySQL instance.
Postgres is real — the migration must be applied before running (the
project's standard docker-compose Postgres at 127.0.0.1:5434).

Skipped unless ``ETL_TEST_DATABASE_URL`` is set, so the suite stays
green in environments without Postgres.
"""

from __future__ import annotations

import os

import pytest
from jp_adopt_api.models import (
    ActivityLog,
    Contact,
    ContactAssignment,
    ContactProfile,
    EtlRun,
    MigrationConflict,
    Outbox,
    StaffIdentityLink,
)
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.orm import sessionmaker

ETL_TEST_DATABASE_URL = os.environ.get(
    "ETL_TEST_DATABASE_URL",
    "postgresql+psycopg2://jp_adopt:jp_adopt@127.0.0.1:5434/jp_adopt",
)


pytestmark = pytest.mark.skipif(
    "ETL_TEST_DATABASE_URL_DISABLE" in os.environ,
    reason="Postgres not available in this environment",
)


# ─── fixtures ──────────────────────────────────────────────────────────────


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
def _cleanup_dt_rows(pg_session):
    """Wipe DT-imported rows between tests so re-runs are deterministic.
    The test fixture data uses post_id range 9000-9999 to avoid colliding
    with any other test suite's data."""
    yield
    # Scope all cleanup to the 9xxx test ID range so we never clobber real
    # DT data the operator may have left behind in the dev DB.
    test_contact_ids = select(Contact.id).where(
        Contact.source_system == "dt", Contact.source_id.like("9%")
    )
    pg_session.execute(
        delete(ActivityLog).where(
            ActivityLog.source_system == "dt",
            ActivityLog.source_id.like("9%")
            | ActivityLog.source_id.like("histlog:9%"),
        )
    )
    pg_session.execute(
        delete(ContactAssignment).where(
            ContactAssignment.contact_id.in_(test_contact_ids)
        )
    )
    pg_session.execute(
        delete(MigrationConflict).where(
            MigrationConflict.source_system == "dt",
            MigrationConflict.source_id.like("9%"),
        )
    )
    pg_session.execute(
        delete(Contact).where(
            Contact.source_system == "dt",
            Contact.source_id.like("9%"),
        )
    )
    pg_session.execute(
        delete(StaffIdentityLink).where(
            StaffIdentityLink.source_system == "dt",
            StaffIdentityLink.dt_user_id.like("9%"),
        )
    )
    # Production-mode tests create etl_run rows with the real table names
    # ('contacts', 'activity_log', etc.). Without cleanup these accumulate
    # across suite runs and confuse `len(runs) >= 1` assertions. The
    # `started_at > now() - interval '5 minutes'` filter limits the wipe
    # to rows created during the current suite run, avoiding any unrelated
    # production etl_run rows.
    pg_session.execute(
        text(
            "DELETE FROM etl_run WHERE "
            "table_name LIKE 'test_%' "
            "OR started_at > now() - interval '5 minutes'"
        )
    )
    pg_session.commit()


# ─── mocked MySQL source ───────────────────────────────────────────────────


class _MockedDtSource:
    """Stand-in for the real ``dt_source`` module. Plugs the same readers
    used by the orchestrator with predetermined row lists."""

    def __init__(
        self,
        *,
        users: list[dict] | None = None,
        contacts: list[dict] | None = None,
        postmeta: dict[int, list[dict]] | None = None,
        comments: list[dict] | None = None,
        activity_log: list[dict] | None = None,
    ) -> None:
        self.users = users or []
        self.contacts = contacts or []
        self.postmeta = postmeta or {}
        self.comments = comments or []
        self.activity_log = activity_log or []

    def iter_activity_log(self, _conn, *, watermark=None, batch_size=500):
        yield from self.activity_log

    def iter_users(self, _conn):
        yield from self.users

    def iter_contacts(self, _conn, *, watermark=None, batch_size=500):
        for row in self.contacts:
            if (
                watermark is not None
                and row.get("post_modified_gmt") is not None
                and row["post_modified_gmt"] <= watermark
            ):
                continue
            yield row

    def load_postmeta(self, _conn, post_ids):
        return {pid: self.postmeta.get(pid, []) for pid in post_ids}

    def iter_comments(self, _conn, *, post_ids=None, watermark=None, batch_size=500):
        for row in self.comments:
            if (
                watermark is not None
                and row.get("comment_date_gmt") is not None
                and row["comment_date_gmt"] <= watermark
            ):
                continue
            yield row

    def iter_p2p(self, _conn, *, p2p_type):
        return iter([])

    def fetch_max_modified(self, _conn, table="wp_posts"):
        # The real reader returns datetime | None; coerce ISO fixture strings
        # so callers (e.g. _min_watermark) get the same type they would in prod.
        def _dt(v):
            from datetime import datetime

            return datetime.fromisoformat(v) if isinstance(v, str) else v

        if table == "wp_posts":
            timestamps = [
                _dt(r["post_modified_gmt"])
                for r in self.contacts
                if r.get("post_modified_gmt")
            ]
            return max(timestamps, default=None)
        if table == "wp_comments":
            timestamps = [
                _dt(r["comment_date_gmt"])
                for r in self.comments
                if r.get("comment_date_gmt")
            ]
            return max(timestamps, default=None)
        return None


def _patch_dt_source(monkeypatch, mock: _MockedDtSource) -> None:
    import jp_adopt_etl.dt_source as src
    import jp_adopt_etl.orchestrator as orch

    monkeypatch.setattr(src, "iter_users", mock.iter_users)
    monkeypatch.setattr(src, "iter_contacts", mock.iter_contacts)
    monkeypatch.setattr(src, "load_postmeta", mock.load_postmeta)
    monkeypatch.setattr(src, "iter_comments", mock.iter_comments)
    monkeypatch.setattr(src, "iter_activity_log", mock.iter_activity_log)
    monkeypatch.setattr(src, "iter_p2p", mock.iter_p2p)
    monkeypatch.setattr(src, "fetch_max_modified", mock.fetch_max_modified)
    # Orchestrator imports the readers directly, so we have to repoint
    # the orchestrator's bindings too.
    monkeypatch.setattr(orch, "iter_users", mock.iter_users)
    monkeypatch.setattr(orch, "iter_contacts", mock.iter_contacts)
    monkeypatch.setattr(orch, "load_postmeta", mock.load_postmeta)
    monkeypatch.setattr(orch, "iter_comments", mock.iter_comments)
    monkeypatch.setattr(orch, "iter_activity_log", mock.iter_activity_log)
    monkeypatch.setattr(orch, "fetch_max_modified", mock.fetch_max_modified)


def _open_engine_returns_pg(monkeypatch, pg_engine):
    """The orchestrator's run_etl calls ``open_engine(mysql_url)`` and
    expects an Engine. We give it a sentinel Postgres engine — the real
    MySQL queries are routed through the monkeypatched readers above, so
    the engine is never queried in practice; it just needs to .connect().
    """
    import jp_adopt_etl.orchestrator as orch

    monkeypatch.setattr(orch, "open_engine", lambda _url: pg_engine)


# ─── tests ─────────────────────────────────────────────────────────────────


def test_import_users_inserts_and_resyncs(monkeypatch, pg_engine, pg_session):
    """Same fixture twice produces inserts then no-op updates; row count
    in StaffIdentityLink lands at exactly the fixture size."""
    from jp_adopt_etl.orchestrator import run_etl

    mock = _MockedDtSource(
        users=[
            {
                "ID": 9001,
                "user_email": "alice@example.com",
                "display_name": "Alice",
                "user_login": "alice",
                "user_status": 0,
            },
            {
                "ID": 9002,
                "user_email": "bob@example.com",
                "display_name": "Bob",
                "user_login": "bob",
                "user_status": 0,
            },
        ]
    )
    _patch_dt_source(monkeypatch, mock)
    _open_engine_returns_pg(monkeypatch, pg_engine)

    first = run_etl(
        mysql_url="mysql+pymysql://ignored",
        postgres_url=ETL_TEST_DATABASE_URL,
        tables=["staff_identity_link"],
        mode="production",
        watermark=None,
    )
    assert first["staff_identity_link"]["rows_in"] == 2

    # Re-run: idempotent — same row count goes through, but no new rows
    # in the table.
    rows = (
        pg_session.execute(
            select(StaffIdentityLink).where(
                StaffIdentityLink.dt_user_id.in_(["9001", "9002"])
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2

    second = run_etl(
        mysql_url="mysql+pymysql://ignored",
        postgres_url=ETL_TEST_DATABASE_URL,
        tables=["staff_identity_link"],
        mode="production",
        watermark=None,
    )
    assert second["staff_identity_link"]["rows_in"] == 2
    rows = (
        pg_session.execute(
            select(StaffIdentityLink).where(
                StaffIdentityLink.dt_user_id.in_(["9001", "9002"])
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2  # still 2, not 4


def test_import_contacts_pivots_postmeta_and_writes_one_bulk_outbox_event(
    monkeypatch, pg_engine, pg_session
):
    """Verifies the full contacts import + outbox_suppressed integration:
    multiple inserted contacts produce exactly one bulk_imported event."""
    from jp_adopt_etl.orchestrator import run_etl

    contacts = [
        {
            "ID": 9101,
            "post_title": "Alice",
            "post_status": "publish",
            "post_date": None,
            "post_date_gmt": None,
        },
        {
            "ID": 9102,
            "post_title": "Bob",
            "post_status": "publish",
            "post_date": None,
            "post_date_gmt": None,
        },
    ]
    postmeta = {
        9101: [
            {"meta_key": "sub_type", "meta_value": "adopter"},
            {"meta_key": "name", "meta_value": "Alice"},
            {"meta_key": "overall_status", "meta_value": "new"},
            {"meta_key": "sources", "meta_value": "website"},
        ],
        9102: [
            {"meta_key": "sub_type", "meta_value": "facilitator"},
            {"meta_key": "name", "meta_value": "Bob"},
            {"meta_key": "overall_status", "meta_value": "active"},  # → ready
        ],
    }
    mock = _MockedDtSource(contacts=contacts, postmeta=postmeta)
    _patch_dt_source(monkeypatch, mock)
    _open_engine_returns_pg(monkeypatch, pg_engine)

    # Capture outbox row count before
    before = pg_session.execute(
        select(Outbox).where(Outbox.event_type == "jp.adopt.v1.bulk_imported")
    ).all()
    before_count = len(before)

    result = run_etl(
        mysql_url="mysql+pymysql://ignored",
        postgres_url=ETL_TEST_DATABASE_URL,
        tables=["contacts"],
        mode="production",
        watermark=None,
    )
    assert result["contacts"]["rows_in"] == 2

    # Two contacts now exist with the expected fields
    rows = (
        pg_session.execute(
            select(Contact).where(Contact.source_id.in_(["9101", "9102"]))
        )
        .scalars()
        .all()
    )
    by_source = {r.source_id: r for r in rows}
    assert by_source["9101"].party_kind == "adopter"
    assert by_source["9101"].adopter_status == "new"
    assert by_source["9101"].origin == "website"
    assert by_source["9102"].party_kind == "facilitator"
    assert by_source["9102"].facilitator_status == "ready"

    # Exactly one bulk_imported outbox row was emitted across the run.
    after = pg_session.execute(
        select(Outbox).where(Outbox.event_type == "jp.adopt.v1.bulk_imported")
    ).all()
    assert len(after) == before_count + 1

    # etl_run captured the watermark
    runs = (
        pg_session.execute(
            select(EtlRun).where(EtlRun.table_name == "contacts").order_by(
                EtlRun.started_at.desc()
            )
        )
        .scalars()
        .all()
    )
    assert runs
    assert runs[0].rows_in == 2
    assert runs[0].mode == "production"


def test_import_contacts_skips_locally_modified_rows(
    monkeypatch, pg_engine, pg_session
):
    """A contact with local_modified_after_import=true is skipped on
    re-run and a migration_conflicts row is recorded."""
    from jp_adopt_etl.orchestrator import run_etl

    # First import: writes the contact
    contacts = [
        {
            "ID": 9201,
            "post_title": "Edited",
            "post_status": "publish",
            "post_date": None,
            "post_date_gmt": None,
        }
    ]
    postmeta = {
        9201: [
            {"meta_key": "type", "meta_value": "adopter"},
            {"meta_key": "contact_email", "meta_value": "edited@example.com"},
            {"meta_key": "overall_status", "meta_value": "new"},
        ]
    }
    mock = _MockedDtSource(contacts=contacts, postmeta=postmeta)
    _patch_dt_source(monkeypatch, mock)
    _open_engine_returns_pg(monkeypatch, pg_engine)

    run_etl(
        mysql_url="mysql+pymysql://ignored",
        postgres_url=ETL_TEST_DATABASE_URL,
        tables=["contacts"],
        mode="production",
        watermark=None,
    )

    # Simulate staff editing in the new system
    pg_session.execute(
        text(
            "UPDATE contacts SET local_modified_after_import = true "
            "WHERE source_system = 'dt' AND source_id = '9201'"
        )
    )
    pg_session.commit()

    # Re-run with updated meta — should NOT clobber the local edit
    postmeta[9201][2]["meta_value"] = "matched"  # would otherwise overwrite
    result = run_etl(
        mysql_url="mysql+pymysql://ignored",
        postgres_url=ETL_TEST_DATABASE_URL,
        tables=["contacts"],
        mode="production",
        watermark=None,
    )
    assert result["contacts"]["rows_in"] == 1
    assert result["contacts"]["rows_out_skipped"] >= 1
    assert result["contacts"]["rows_in_conflict"] >= 1

    # Verify the row in DB still has adopter_status='new' (the local edit)
    contact = pg_session.execute(
        select(Contact).where(
            Contact.source_system == "dt", Contact.source_id == "9201"
        )
    ).scalar_one()
    assert contact.adopter_status == "new"


def test_import_contacts_populates_contact_profile(monkeypatch, pg_engine, pg_session):
    """A contact with JP-custom adoption postmeta gets a contact_profile row."""
    import phpserialize

    from jp_adopt_etl.orchestrator import run_etl

    contacts = [
        {
            "ID": 9301,
            "post_title": "Profiled Org",
            "post_status": "publish",
            "post_date": None,
            "post_date_gmt": None,
        }
    ]
    postmeta = {
        9301: [
            {"meta_key": "sub_type", "meta_value": "adopter"},
            {"meta_key": "overall_status", "meta_value": "new"},
            {"meta_key": "adopter_type", "meta_value": "organization"},
            {"meta_key": "entity_size", "meta_value": "101_500"},
            {"meta_key": "website", "meta_value": "https://org.example"},
            {
                "meta_key": "ministry_areas",
                "meta_value": phpserialize.dumps(["prayer", "training"]).decode(
                    "utf-8"
                ),
            },
            {"meta_key": "has_doctrinal_distinctives", "meta_value": "1"},
        ],
    }
    mock = _MockedDtSource(contacts=contacts, postmeta=postmeta)
    _patch_dt_source(monkeypatch, mock)
    _open_engine_returns_pg(monkeypatch, pg_engine)

    run_etl(
        mysql_url="mysql+pymysql://ignored",
        postgres_url=ETL_TEST_DATABASE_URL,
        tables=["contacts"],
        mode="production",
        watermark=None,
    )

    contact = pg_session.execute(
        select(Contact).where(
            Contact.source_system == "dt", Contact.source_id == "9301"
        )
    ).scalar_one()
    profile = pg_session.execute(
        select(ContactProfile).where(ContactProfile.contact_id == contact.id)
    ).scalar_one()
    assert profile.adopter_type == "organization"
    assert profile.entity_size == "101_500"
    assert profile.website == "https://org.example"
    assert profile.ministry_areas == ["prayer", "training"]
    assert profile.has_doctrinal_distinctives is True

    # Idempotent: a second run updates in place (no duplicate-key error).
    run_etl(
        mysql_url="mysql+pymysql://ignored",
        postgres_url=ETL_TEST_DATABASE_URL,
        tables=["contacts"],
        mode="production",
        watermark=None,
    )
    count = pg_session.execute(
        select(ContactProfile).where(ContactProfile.contact_id == contact.id)
    ).all()
    assert len(count) == 1


def test_import_interests_from_fpg_submission_data(monkeypatch, pg_engine, pg_session):
    """Per-FPG interests are parsed from fpg_submission_data and upserted
    idempotently; an unknown people_id3 is skipped + recorded as a conflict."""
    from jp_adopt_api.models import AdopterInterest, MigrationConflict

    from jp_adopt_etl.orchestrator import run_etl

    pg_session.execute(
        text(
            "INSERT INTO fpg (people_id3, name, frontier) VALUES "
            "('88001', 'Test FPG', true) ON CONFLICT (people_id3) DO NOTHING"
        )
    )
    pg_session.commit()

    # Second element's people_id3 (99999) is not in fpg → skipped + conflict.
    fpg_json = (
        '[{"peopleId3":88001,"engagementStatus":"ready","canFacilitate":true,'
        '"facilitationServices":["prayer_updates"],"networkServices":[],'
        '"commitmentTypes":["pray"]},'
        '{"peopleId3":99999,"engagementStatus":"potential",'
        '"facilitationServices":[],"networkServices":[],"commitmentTypes":[]}]'
    )
    contacts = [
        {
            "ID": 9501,
            "post_title": "Interested",
            "post_status": "publish",
            "post_date": None,
            "post_date_gmt": None,
        }
    ]
    postmeta = {
        9501: [
            {"meta_key": "sub_type", "meta_value": "adopter"},
            {"meta_key": "overall_status", "meta_value": "new"},
            {"meta_key": "fpg_submission_data", "meta_value": fpg_json},
        ],
    }
    mock = _MockedDtSource(contacts=contacts, postmeta=postmeta)
    _patch_dt_source(monkeypatch, mock)
    _open_engine_returns_pg(monkeypatch, pg_engine)

    try:
        result = run_etl(
            mysql_url="mysql+pymysql://ignored",
            postgres_url=ETL_TEST_DATABASE_URL,
            tables=["contacts", "adopter_interest"],
            mode="production",
            watermark=None,
        )
        assert result["adopter_interest"]["rows_in"] == 2
        assert result["adopter_interest"]["rows_out_inserted"] == 1
        assert result["adopter_interest"]["rows_out_skipped"] == 1

        contact = pg_session.execute(
            select(Contact).where(
                Contact.source_system == "dt", Contact.source_id == "9501"
            )
        ).scalar_one()
        interests = pg_session.execute(
            select(AdopterInterest).where(AdopterInterest.contact_id == contact.id)
        ).scalars().all()
        assert len(interests) == 1
        assert interests[0].people_id3 == "88001"
        assert interests[0].engagement_status == "ready"
        assert interests[0].facilitation_services == ["prayer_updates"]
        assert interests[0].source_id == "9501:88001"

        # Unknown people_id3 recorded as a conflict.
        conflicts = pg_session.execute(
            select(MigrationConflict).where(
                MigrationConflict.table_name == "adopter_interest",
                MigrationConflict.source_id == "9501:99999",
            )
        ).scalars().all()
        assert len(conflicts) == 1
        assert conflicts[0].conflict_type == "fpg_not_found"

        # Idempotent re-run: still exactly one interest row.
        run_etl(
            mysql_url="mysql+pymysql://ignored",
            postgres_url=ETL_TEST_DATABASE_URL,
            tables=["contacts", "adopter_interest"],
            mode="production",
            watermark=None,
        )
        interests = pg_session.execute(
            select(AdopterInterest).where(AdopterInterest.contact_id == contact.id)
        ).scalars().all()
        assert len(interests) == 1
    finally:
        pg_session.execute(
            delete(AdopterInterest).where(AdopterInterest.source_system == "dt")
        )
        pg_session.execute(
            text("DELETE FROM migration_conflicts WHERE source_id LIKE '9501:%'")
        )
        pg_session.execute(text("DELETE FROM fpg WHERE people_id3 = '88001'"))
        pg_session.commit()


def test_import_comments_resolves_threading(monkeypatch, pg_engine, pg_session):
    """A reply comment's parent_id resolves to its parent's new UUID."""
    from jp_adopt_etl.orchestrator import run_etl

    contacts = [
        {
            "ID": 9601,
            "post_title": "Threaded",
            "post_status": "publish",
            "post_date": None,
            "post_date_gmt": None,
        }
    ]
    postmeta = {9601: [{"meta_key": "sub_type", "meta_value": "adopter"}]}
    comments = [
        {
            "comment_ID": 700,
            "comment_post_ID": 9601,
            "comment_author": "Staff",
            "comment_author_email": "s@x.dev",
            "comment_date": None,
            "comment_date_gmt": "2026-01-01T10:00:00",
            "comment_content": "Parent note",
            "comment_type": "",
            "comment_parent": 0,
            "user_id": 0,
            "comment_agent": "Mozilla/5.0",
            "comment_approved": "1",
        },
        {
            "comment_ID": 701,
            "comment_post_ID": 9601,
            "comment_author": "Staff",
            "comment_author_email": "s@x.dev",
            "comment_date": None,
            "comment_date_gmt": "2026-01-02T10:00:00",
            "comment_content": "Reply note",
            "comment_type": "",
            "comment_parent": 700,
            "user_id": 0,
            "comment_agent": "Mozilla/5.0",
            "comment_approved": "1",
        },
    ]
    mock = _MockedDtSource(contacts=contacts, postmeta=postmeta, comments=comments)
    _patch_dt_source(monkeypatch, mock)
    _open_engine_returns_pg(monkeypatch, pg_engine)

    run_etl(
        mysql_url="mysql+pymysql://ignored",
        postgres_url=ETL_TEST_DATABASE_URL,
        tables=["contacts", "activity_log"],
        mode="production",
        watermark=None,
    )

    rows = pg_session.execute(
        select(ActivityLog).where(ActivityLog.source_system == "dt")
    ).scalars().all()
    by_source = {r.source_id: r for r in rows}
    parent = by_source["700"]
    child = by_source["701"]
    assert child.parent_id == parent.id
    assert parent.parent_id is None


def test_import_activity_history_into_activity_log(monkeypatch, pg_engine, pg_session):
    """A wp_dt_activity_log field_update row becomes a kind='field_change'
    activity_log entry alongside comments."""
    from jp_adopt_etl.orchestrator import run_etl

    contacts = [
        {
            "ID": 9701,
            "post_title": "Audited",
            "post_status": "publish",
            "post_date": None,
            "post_date_gmt": None,
        }
    ]
    postmeta = {9701: [{"meta_key": "sub_type", "meta_value": "adopter"}]}
    activity_log = [
        {
            "histid": 9001,
            "action": "field_update",
            "object_type": "contacts",
            "object_id": 9701,
            "user_id": 0,
            "hist_time": 1700000000,
            "meta_key": "overall_status",
            "old_value": "new",
            "meta_value": "active",
            "field_type": "key_select",
        }
    ]
    mock = _MockedDtSource(
        contacts=contacts, postmeta=postmeta, activity_log=activity_log
    )
    _patch_dt_source(monkeypatch, mock)
    _open_engine_returns_pg(monkeypatch, pg_engine)

    run_etl(
        mysql_url="mysql+pymysql://ignored",
        postgres_url=ETL_TEST_DATABASE_URL,
        tables=["contacts", "activity_log"],
        mode="production",
        watermark=None,
    )

    row = pg_session.execute(
        select(ActivityLog).where(
            ActivityLog.source_system == "dt",
            ActivityLog.source_id == "histlog:9001",
        )
    ).scalar_one()
    assert row.kind == "field_change"
    assert row.body == "overall_status changed from 'new' to 'active'"


def test_import_assignment_resolves_subject_and_records_conflict(
    monkeypatch, pg_engine, pg_session
):
    """assigned_to resolves to a B2C subject via staff_identity_link; an
    assignee without a subject is skipped + recorded as a conflict."""
    import uuid as _uuid

    from jp_adopt_api.models import (
        ContactAssignment,
        MigrationConflict,
        StaffIdentityLink,
    )

    from jp_adopt_etl.orchestrator import run_etl

    # Staff member who has signed in (has a B2C subject).
    pg_session.add(
        StaffIdentityLink(
            id=_uuid.uuid4(),
            dt_user_id="9802",
            b2c_subject_id="sub-9802",
            email="staff@x.dev",
            email_normalized="staff@x.dev",
            status="active",
            source_system="dt",
        )
    )
    pg_session.commit()

    contacts = [
        {"ID": 9801, "post_title": "Owned", "post_status": "publish",
         "post_date": None, "post_date_gmt": None},
        {"ID": 9803, "post_title": "OwnedByGhost", "post_status": "publish",
         "post_date": None, "post_date_gmt": None},
    ]
    postmeta = {
        9801: [
            {"meta_key": "sub_type", "meta_value": "adopter"},
            {"meta_key": "assigned_to", "meta_value": "user-9802"},
        ],
        9803: [
            {"meta_key": "sub_type", "meta_value": "adopter"},
            {"meta_key": "assigned_to", "meta_value": "user-9999"},  # no link
        ],
    }
    mock = _MockedDtSource(contacts=contacts, postmeta=postmeta)
    _patch_dt_source(monkeypatch, mock)
    _open_engine_returns_pg(monkeypatch, pg_engine)

    try:
        result = run_etl(
            mysql_url="mysql+pymysql://ignored",
            postgres_url=ETL_TEST_DATABASE_URL,
            tables=["contacts", "contact_assignment"],
            mode="production",
            watermark=None,
        )
        assert result["contact_assignment"]["rows_in"] == 2
        assert result["contact_assignment"]["rows_out_inserted"] == 1
        assert result["contact_assignment"]["rows_out_skipped"] == 1

        owned = pg_session.execute(
            select(Contact).where(Contact.source_id == "9801")
        ).scalar_one()
        assignment = pg_session.execute(
            select(ContactAssignment).where(
                ContactAssignment.contact_id == owned.id
            )
        ).scalar_one()
        assert assignment.user_subject_id == "sub-9802"

        conflicts = pg_session.execute(
            select(MigrationConflict).where(
                MigrationConflict.table_name == "contact_assignment",
                MigrationConflict.source_id == "9803",
            )
        ).scalars().all()
        assert len(conflicts) == 1
        assert conflicts[0].conflict_type == "assignee_no_subject"
    finally:
        pg_session.execute(
            text("DELETE FROM migration_conflicts WHERE source_id IN ('9801', '9803')")
        )
        pg_session.commit()


def test_duplicate_email_keeps_contact_clears_email_records_conflict(
    monkeypatch, pg_engine, pg_session
):
    """DT permits multiple contacts to share an email; the new system's
    partial unique index on contacts.email_normalized does not. The ETL
    must keep the contact, drop the colliding email, and record a
    duplicate_email migration_conflicts row for review."""
    from jp_adopt_api.models import MigrationConflict

    from jp_adopt_etl.orchestrator import run_etl

    contacts = [
        {"ID": 9111, "post_title": "First", "post_status": "publish",
         "post_date": None, "post_date_gmt": None},
        {"ID": 9112, "post_title": "Second", "post_status": "publish",
         "post_date": None, "post_date_gmt": None},
    ]
    postmeta = {
        9111: [
            {"meta_key": "sub_type", "meta_value": "adopter"},
            {"meta_key": "overall_status", "meta_value": "new"},
            {"meta_key": "contact_email_aaa", "meta_value": "dup@example.com"},
        ],
        9112: [
            {"meta_key": "sub_type", "meta_value": "adopter"},
            {"meta_key": "overall_status", "meta_value": "new"},
            {"meta_key": "contact_email_bbb", "meta_value": "dup@example.com"},
        ],
    }
    mock = _MockedDtSource(contacts=contacts, postmeta=postmeta)
    _patch_dt_source(monkeypatch, mock)
    _open_engine_returns_pg(monkeypatch, pg_engine)

    try:
        result = run_etl(
            mysql_url="mysql+pymysql://ignored",
            postgres_url=ETL_TEST_DATABASE_URL,
            tables=["contacts"],
            mode="production",
            watermark=None,
        )
        assert result["contacts"]["rows_in"] == 2
        assert result["contacts"]["rows_in_conflict"] >= 1

        # Both contacts persisted; one kept the email, one had it cleared.
        rows = pg_session.execute(
            select(Contact).where(Contact.source_id.in_(["9111", "9112"]))
        ).scalars().all()
        assert len(rows) == 2
        emails = sorted(
            [r.email_normalized for r in rows], key=lambda v: (v is None, v)
        )
        assert emails == ["dup@example.com", None]

        # Conflict captured for review.
        conflicts = pg_session.execute(
            select(MigrationConflict).where(
                MigrationConflict.table_name == "contacts",
                MigrationConflict.conflict_type == "duplicate_email",
                MigrationConflict.source_id.in_(["9111", "9112"]),
            )
        ).scalars().all()
        assert len(conflicts) == 1
        assert conflicts[0].source_value == {"email_normalized": "dup@example.com"}
    finally:
        pg_session.execute(
            text(
                "DELETE FROM migration_conflicts "
                "WHERE source_id IN ('9111', '9112')"
            )
        )
        pg_session.commit()


def test_dry_run_is_non_mutating_but_writes_etl_run(monkeypatch, pg_engine, pg_session):
    """--mode dry_run persists no data rows but still writes etl_run +
    migration_conflicts audit rows so an operator can triage the would-be
    impact without leaving production-shaped data behind."""
    from jp_adopt_etl.orchestrator import run_etl

    contacts = [
        # Two contacts colliding on the same email — production-mode would
        # record a duplicate_email conflict. Dry-run must do the same.
        {"ID": 9901, "post_title": "Ghost", "post_status": "publish",
         "post_date": None, "post_date_gmt": None},
        {"ID": 9902, "post_title": "Ghost Twin", "post_status": "publish",
         "post_date": None, "post_date_gmt": None},
    ]
    postmeta = {
        9901: [
            {"meta_key": "sub_type", "meta_value": "adopter"},
            {"meta_key": "overall_status", "meta_value": "new"},
            {"meta_key": "contact_email_aaa", "meta_value": "ghost@x.dev"},
        ],
        9902: [
            {"meta_key": "sub_type", "meta_value": "adopter"},
            {"meta_key": "overall_status", "meta_value": "new"},
            {"meta_key": "contact_email_bbb", "meta_value": "ghost@x.dev"},
        ],
    }
    mock = _MockedDtSource(contacts=contacts, postmeta=postmeta)
    _patch_dt_source(monkeypatch, mock)
    _open_engine_returns_pg(monkeypatch, pg_engine)

    try:
        run_etl(
            mysql_url="mysql+pymysql://ignored",
            postgres_url=ETL_TEST_DATABASE_URL,
            tables=["contacts"],
            mode="dry_run",
            watermark=None,
        )
        # No contact was persisted.
        ghost = pg_session.execute(
            select(Contact).where(Contact.source_id.in_(["9901", "9902"]))
        ).scalars().all()
        assert ghost == []
        # But the etl_run audit row was.
        runs = pg_session.execute(
            select(EtlRun).where(
                EtlRun.mode == "dry_run", EtlRun.table_name == "contacts"
            )
        ).scalars().all()
        assert len(runs) >= 1
        assert runs[-1].rows_in == 2
        # AND the duplicate_email conflict was preserved through the rollback.
        conflicts = pg_session.execute(
            select(MigrationConflict).where(
                MigrationConflict.table_name == "contacts",
                MigrationConflict.conflict_type == "duplicate_email",
                MigrationConflict.source_id.in_(["9901", "9902"]),
            )
        ).scalars().all()
        assert len(conflicts) == 1
    finally:
        pg_session.execute(text("DELETE FROM etl_run WHERE mode = 'dry_run'"))
        pg_session.execute(
            text(
                "DELETE FROM migration_conflicts "
                "WHERE source_id IN ('9901', '9902')"
            )
        )
        pg_session.commit()


def test_import_assignment_protects_local_overrides(
    monkeypatch, pg_engine, pg_session
):
    """A staff reassignment in jp-adopt-core (assigned_by != 'dt_import')
    is preserved across a delta re-run — DT does not clobber it."""
    import uuid as _uuid

    from jp_adopt_api.models import StaffIdentityLink

    from jp_adopt_etl.orchestrator import run_etl

    # Two staff. Both signed into B2C.
    pg_session.add_all([
        StaffIdentityLink(
            id=_uuid.uuid4(), dt_user_id="9810", b2c_subject_id="sub-9810",
            email="dt@x.dev", email_normalized="dt@x.dev",
            status="active", source_system="dt",
        ),
        StaffIdentityLink(
            id=_uuid.uuid4(), dt_user_id="9811", b2c_subject_id="sub-9811",
            email="local@x.dev", email_normalized="local@x.dev",
            status="active", source_system="dt",
        ),
    ])
    pg_session.commit()

    contacts = [
        {"ID": 9820, "post_title": "Owned", "post_status": "publish",
         "post_date": None, "post_date_gmt": None},
    ]
    postmeta = {
        9820: [
            {"meta_key": "sub_type", "meta_value": "adopter"},
            {"meta_key": "assigned_to", "meta_value": "user-9810"},
        ],
    }
    mock = _MockedDtSource(contacts=contacts, postmeta=postmeta)
    _patch_dt_source(monkeypatch, mock)
    _open_engine_returns_pg(monkeypatch, pg_engine)

    try:
        # 1) Initial DT import sets assignment to sub-9810 with assigned_by='dt_import'.
        run_etl(
            mysql_url="mysql+pymysql://ignored",
            postgres_url=ETL_TEST_DATABASE_URL,
            tables=["contacts", "contact_assignment"],
            mode="production",
            watermark=None,
        )
        contact = pg_session.execute(
            select(Contact).where(Contact.source_id == "9820")
        ).scalar_one()
        assignment = pg_session.execute(
            select(ContactAssignment).where(
                ContactAssignment.contact_id == contact.id
            )
        ).scalar_one()
        assert assignment.user_subject_id == "sub-9810"
        assert assignment.assigned_by == "dt_import"

        # 2) Staff reassigns in jp-adopt-core via the API (out of band).
        pg_session.execute(
            text(
                "UPDATE contact_assignment SET user_subject_id='sub-9811', "
                "assigned_by='staff_action' WHERE contact_id=:cid"
            ),
            {"cid": contact.id},
        )
        pg_session.commit()

        # 3) Delta re-run of the ETL. DT still says assigned_to='user-9810'.
        result = run_etl(
            mysql_url="mysql+pymysql://ignored",
            postgres_url=ETL_TEST_DATABASE_URL,
            tables=["contact_assignment"],
            mode="production",
            watermark=None,
        )

        # Staff override held; conflict recorded for operator review.
        pg_session.expire_all()
        kept = pg_session.execute(
            select(ContactAssignment).where(
                ContactAssignment.contact_id == contact.id
            )
        ).scalar_one()
        assert kept.user_subject_id == "sub-9811"
        assert kept.assigned_by == "staff_action"
        assert result["contact_assignment"]["rows_out_skipped"] == 1

        conflicts = pg_session.execute(
            select(MigrationConflict).where(
                MigrationConflict.table_name == "contact_assignment",
                MigrationConflict.conflict_type == "local_assignment_override",
                MigrationConflict.source_id == "9820",
            )
        ).scalars().all()
        assert len(conflicts) == 1
    finally:
        pg_session.execute(
            text("DELETE FROM migration_conflicts WHERE source_id = '9820'")
        )
        pg_session.commit()


def test_migration_conflicts_dedup_across_delta_runs(
    monkeypatch, pg_engine, pg_session
):
    """A recurring conflict (e.g. the same DT contact repeatedly losing
    to a local_assignment_override) must record only ONE row across N
    delta runs — migration 0025's partial unique index + ON CONFLICT
    DO NOTHING make _record_conflict idempotent."""
    import uuid as _uuid

    from jp_adopt_api.models import StaffIdentityLink

    from jp_adopt_etl.orchestrator import run_etl

    pg_session.add(
        StaffIdentityLink(
            id=_uuid.uuid4(), dt_user_id="9830", b2c_subject_id="sub-9830",
            email="dup@x.dev", email_normalized="dup@x.dev",
            status="active", source_system="dt",
        ),
    )
    pg_session.commit()

    contacts = [
        {"ID": 9840, "post_title": "Recurring", "post_status": "publish",
         "post_date": None, "post_date_gmt": None},
    ]
    postmeta = {
        9840: [
            {"meta_key": "sub_type", "meta_value": "adopter"},
            {"meta_key": "assigned_to", "meta_value": "user-9830"},
        ],
    }
    mock = _MockedDtSource(contacts=contacts, postmeta=postmeta)
    _patch_dt_source(monkeypatch, mock)
    _open_engine_returns_pg(monkeypatch, pg_engine)

    try:
        # 1) Initial DT import.
        run_etl(
            mysql_url="mysql+pymysql://ignored",
            postgres_url=ETL_TEST_DATABASE_URL,
            tables=["contacts", "contact_assignment"],
            mode="production",
            watermark=None,
        )
        contact = pg_session.execute(
            select(Contact).where(Contact.source_id == "9840")
        ).scalar_one()

        # 2) Staff override.
        pg_session.execute(
            text(
                "UPDATE contact_assignment SET assigned_by='staff_action' "
                "WHERE contact_id=:cid"
            ),
            {"cid": contact.id},
        )
        pg_session.commit()

        # 3) Run the delta three times. Each run detects the override and
        # would record a conflict — without the dedup index this grows
        # linearly with runs.
        for _ in range(3):
            run_etl(
                mysql_url="mysql+pymysql://ignored",
                postgres_url=ETL_TEST_DATABASE_URL,
                tables=["contact_assignment"],
                mode="production",
                watermark=None,
            )

        conflicts = pg_session.execute(
            select(MigrationConflict).where(
                MigrationConflict.table_name == "contact_assignment",
                MigrationConflict.conflict_type == "local_assignment_override",
                MigrationConflict.source_id == "9840",
            )
        ).scalars().all()
        # Exactly one row even though the conflict was detected on each
        # of the three delta runs.
        assert len(conflicts) == 1
    finally:
        pg_session.execute(
            text("DELETE FROM migration_conflicts WHERE source_id = '9840'")
        )
        pg_session.commit()


def test_dry_run_exception_path_preserves_audit(
    monkeypatch, pg_engine, pg_session
):
    """A dry-run that raises mid-loop (e.g. UnmappedStatusError in a later
    table) still commits the audit trail for tables that completed before
    the failure. The exception propagates after the replay."""
    from jp_adopt_etl.mappers.status import UnmappedStatusError
    from jp_adopt_etl.orchestrator import run_etl

    # First table: a contact with a status we *don't* map (forces
    # UnmappedStatusError in dry_run).
    contacts = [
        {"ID": 9905, "post_title": "Bad", "post_status": "publish",
         "post_date": None, "post_date_gmt": None},
    ]
    postmeta = {
        9905: [
            {"meta_key": "sub_type", "meta_value": "adopter"},
            {"meta_key": "overall_status", "meta_value": "totally_unknown"},
        ],
    }
    mock = _MockedDtSource(contacts=contacts, postmeta=postmeta)
    _patch_dt_source(monkeypatch, mock)
    _open_engine_returns_pg(monkeypatch, pg_engine)

    try:
        with pytest.raises(UnmappedStatusError):
            run_etl(
                mysql_url="mysql+pymysql://ignored",
                postgres_url=ETL_TEST_DATABASE_URL,
                tables=["contacts"],
                mode="dry_run",
                watermark=None,
            )

        # The conflict for the unmapped status was recorded *before* the
        # exception re-raised; the exception-path replay preserved it.
        conflicts = pg_session.execute(
            select(MigrationConflict).where(
                MigrationConflict.source_id == "9905",
                MigrationConflict.conflict_type.like("unmapped_status:%"),
            )
        ).scalars().all()
        assert len(conflicts) == 1

        # No contact row persisted (dry-run is non-mutating).
        ghost = pg_session.execute(
            select(Contact).where(Contact.source_id == "9905")
        ).scalar_one_or_none()
        assert ghost is None
    finally:
        pg_session.execute(
            text("DELETE FROM migration_conflicts WHERE source_id = '9905'")
        )
        pg_session.commit()


def test_full_run_records_deleted_in_source(monkeypatch, pg_engine, pg_session):
    """A contact imported on a prior full run but absent from a later
    snapshot is recorded in etl_deleted_in_source (idempotently)."""
    from jp_adopt_api.models import EtlDeletedInSource

    from jp_adopt_etl.orchestrator import run_etl

    present = _MockedDtSource(
        contacts=[
            {"ID": 9011, "post_title": "Here", "post_status": "publish",
             "post_date": None, "post_date_gmt": None}
        ],
        postmeta={9011: [{"meta_key": "sub_type", "meta_value": "adopter"}]},
    )
    _patch_dt_source(monkeypatch, present)
    _open_engine_returns_pg(monkeypatch, pg_engine)
    run_etl(
        mysql_url="mysql+pymysql://ignored",
        postgres_url=ETL_TEST_DATABASE_URL,
        tables=["contacts"],
        mode="production",
        watermark=None,
    )

    try:
        # Later snapshot no longer contains 9011.
        gone = _MockedDtSource(contacts=[], postmeta={})
        _patch_dt_source(monkeypatch, gone)
        _open_engine_returns_pg(monkeypatch, pg_engine)
        run_etl(
            mysql_url="mysql+pymysql://ignored",
            postgres_url=ETL_TEST_DATABASE_URL,
            tables=["contacts"],
            mode="production",
            watermark=None,
        )
        rows = pg_session.execute(
            select(EtlDeletedInSource).where(
                EtlDeletedInSource.source_system == "dt",
                EtlDeletedInSource.source_id == "9011",
            )
        ).scalars().all()
        assert len(rows) == 1

        # Idempotent: a third full run does not duplicate the record.
        run_etl(
            mysql_url="mysql+pymysql://ignored",
            postgres_url=ETL_TEST_DATABASE_URL,
            tables=["contacts"],
            mode="production",
            watermark=None,
        )
        rows = pg_session.execute(
            select(EtlDeletedInSource).where(
                EtlDeletedInSource.source_system == "dt",
                EtlDeletedInSource.source_id == "9011",
            )
        ).scalars().all()
        assert len(rows) == 1
    finally:
        pg_session.execute(
            text("DELETE FROM etl_deleted_in_source WHERE source_id = '9011'")
        )
        pg_session.commit()


def test_resolve_auto_watermark(monkeypatch, pg_engine, pg_session):
    """`resolve_auto_watermark` returns MIN(MAX(source_max_modified_at))
    per table across prior successful production runs. Failed runs
    (errors > 0) and dry-runs are excluded.
    """
    import uuid as _uuid
    from datetime import UTC, datetime

    from jp_adopt_etl.orchestrator import resolve_auto_watermark

    # Snapshot of pre-existing watermark, so the test is order-independent
    # against any rows the rest of the suite happened to leave behind.
    baseline = resolve_auto_watermark(ETL_TEST_DATABASE_URL)

    t_contacts = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    t_activity = datetime(2026, 6, 4, 12, 30, tzinfo=UTC)  # later
    t_failed = datetime(2026, 6, 4, 13, 0, tzinfo=UTC)  # never picked (errors)
    t_dry = datetime(2026, 6, 4, 14, 0, tzinfo=UTC)  # never picked (mode)

    runs = [
        # Successful production runs — the MIN(MAX(...)) should be t_contacts.
        EtlRun(
            id=_uuid.uuid4(), table_name="test_contacts_a",
            mode="production", started_at=t_contacts,
            ended_at=t_contacts, source_max_modified_at=t_contacts,
            rows_in=1, rows_out_inserted=1, rows_out_updated=0,
            rows_out_skipped=0, rows_in_conflict=0, errors=0,
        ),
        EtlRun(
            id=_uuid.uuid4(), table_name="test_activity_a",
            mode="production", started_at=t_activity,
            ended_at=t_activity, source_max_modified_at=t_activity,
            rows_in=1, rows_out_inserted=1, rows_out_updated=0,
            rows_out_skipped=0, rows_in_conflict=0, errors=0,
        ),
        # Should be ignored: errors > 0.
        EtlRun(
            id=_uuid.uuid4(), table_name="test_contacts_b",
            mode="production", started_at=t_failed,
            ended_at=t_failed, source_max_modified_at=t_failed,
            rows_in=1, rows_out_inserted=0, rows_out_updated=0,
            rows_out_skipped=1, rows_in_conflict=0, errors=1,
        ),
        # Should be ignored: dry_run.
        EtlRun(
            id=_uuid.uuid4(), table_name="test_contacts_c",
            mode="dry_run", started_at=t_dry,
            ended_at=t_dry, source_max_modified_at=t_dry,
            rows_in=1, rows_out_inserted=1, rows_out_updated=0,
            rows_out_skipped=0, rows_in_conflict=0, errors=0,
        ),
    ]
    pg_session.add_all(runs)
    pg_session.commit()

    try:
        watermark = resolve_auto_watermark(ETL_TEST_DATABASE_URL)
        # Two paths produce a valid result depending on whether the dev
        # DB had earlier production-mode rows from other tests:
        # - baseline is None OR baseline >= t_contacts → expected is t_contacts
        #   (our injected rows shifted the MIN).
        # - baseline < t_contacts → baseline stays the MIN regardless of
        #   what we inserted (the function correctly excluded older
        #   non-test rows being earlier than the test_contacts_b errors=1
        #   filter, etc.).
        expected = (
            min(baseline, t_contacts) if baseline is not None else t_contacts
        )
        # Postgres TIMESTAMPTZ + psycopg2 round-trip preserves microseconds
        # losslessly; tolerate at most 1ms drift (effectively just floating
        # point comparison guard).
        assert (
            abs((watermark - expected).total_seconds()) < 0.001
        ), f"got {watermark}, expected {expected}"
        # Independently assert that the ignored rows (errors=1 + dry_run)
        # did NOT contribute, regardless of baseline.
        assert (
            watermark < t_failed and watermark < t_dry
        ), f"errors=1 or dry_run row leaked into watermark: {watermark}"
    finally:
        pg_session.execute(
            text(
                "DELETE FROM etl_run WHERE table_name "
                "IN ('test_contacts_a', 'test_activity_a', "
                "'test_contacts_b', 'test_contacts_c')"
            )
        )
        pg_session.commit()


def test_resolve_cli_watermark_none_passes_through():
    """`_resolve_cli_watermark(None, ...)` returns None without touching
    Postgres (no etl_run lookup needed for the full-scan path)."""
    from jp_adopt_etl.orchestrator import _resolve_cli_watermark

    # Postgres URL is a placeholder — function MUST NOT connect.
    assert _resolve_cli_watermark(None, "postgresql://invalid") is None


def test_resolve_cli_watermark_iso8601_parses_to_utc():
    """A valid ISO 8601 string is parsed and anchored to UTC. No DB
    lookup required for explicit timestamps."""
    from datetime import UTC, datetime

    from jp_adopt_etl.orchestrator import _resolve_cli_watermark

    result = _resolve_cli_watermark(
        "2026-06-04T12:00:00", "postgresql://invalid"
    )
    assert result == datetime(2026, 6, 4, 12, 0, tzinfo=UTC)


def test_resolve_cli_watermark_malformed_iso_exits_with_code_2(caplog):
    """Garbage non-auto, non-ISO input exits with code 2 (mirroring
    argparse's error contract) and logs a clear error message — NOT a
    raw traceback with exit code 1."""
    from jp_adopt_etl.orchestrator import _resolve_cli_watermark

    with pytest.raises(SystemExit) as exc_info:
        _resolve_cli_watermark("not-a-date", "postgresql://invalid")
    assert exc_info.value.code == 2
    assert "Invalid --watermark value" in caplog.text


def test_resolve_cli_watermark_auto_delegates_to_resolve_auto_watermark(
    monkeypatch,
):
    """`_resolve_cli_watermark('auto', url)` dispatches to
    resolve_auto_watermark with the same URL and returns its result.
    No DB touch required — verify via monkeypatch."""
    from datetime import UTC, datetime

    from jp_adopt_etl import orchestrator as orch

    received: list[str] = []
    expected = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)

    def fake(url: str) -> datetime:
        received.append(url)
        return expected

    monkeypatch.setattr(orch, "resolve_auto_watermark", fake)
    result = orch._resolve_cli_watermark("auto", "postgresql://sentinel")
    assert result == expected
    assert received == ["postgresql://sentinel"]
