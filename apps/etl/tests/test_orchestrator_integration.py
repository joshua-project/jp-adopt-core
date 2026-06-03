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
    ContactProfile,
    EtlRun,
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
    pg_session.execute(
        delete(ActivityLog).where(ActivityLog.source_system == "dt")
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
    pg_session.execute(
        text("DELETE FROM etl_run WHERE table_name LIKE 'test_%'")
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
    ) -> None:
        self.users = users or []
        self.contacts = contacts or []
        self.postmeta = postmeta or {}
        self.comments = comments or []

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
        if table == "wp_posts":
            timestamps = [
                r["post_modified_gmt"]
                for r in self.contacts
                if r.get("post_modified_gmt")
            ]
            return max(timestamps, default=None)
        if table == "wp_comments":
            timestamps = [
                r["comment_date_gmt"]
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
    monkeypatch.setattr(src, "iter_p2p", mock.iter_p2p)
    monkeypatch.setattr(src, "fetch_max_modified", mock.fetch_max_modified)
    # Orchestrator imports the readers directly, so we have to repoint
    # the orchestrator's bindings too.
    monkeypatch.setattr(orch, "iter_users", mock.iter_users)
    monkeypatch.setattr(orch, "iter_contacts", mock.iter_contacts)
    monkeypatch.setattr(orch, "load_postmeta", mock.load_postmeta)
    monkeypatch.setattr(orch, "iter_comments", mock.iter_comments)
    monkeypatch.setattr(orch, "iter_p2p", mock.iter_p2p)
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
