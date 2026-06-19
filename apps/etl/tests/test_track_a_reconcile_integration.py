"""Track A duplicate_email reconciliation — integration tests.

Real Postgres (migrations must be applied; project dev DB at
127.0.0.1:5434), MOCKED DT source (never a live MySQL connection). All
fixture data is scoped to the source_id 9xxx range and wiped after each
test, mirroring ``test_orchestrator_integration.py``.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
from jp_adopt_api.models import (
    ActivityLog,
    AdopterInterest,
    Consent,
    Contact,
    ContactAssignment,
    ContactProfile,
    EtlRun,
    FacilitatingOrg,
    Fpg,
    IdentityLink,
    Match,
    MigrationConflict,
)
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.orm import sessionmaker

from jp_adopt_etl.reconcile.track_a_duplicate_email import reconcile

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
def _cleanup(pg_session):
    """Wipe all 9xxx-range fixture rows before and after each test."""
    def _wipe():
        test_contact_ids = select(Contact.id).where(
            Contact.email_normalized.like("%@9test.dev")
        )
        scoped_contact_ids = select(Contact.id).where(
            Contact.source_id.like("9%"),
            Contact.source_system.in_(["dt", "forms", "staff_seed"]),
        )
        # Matches reference adopter_interest rows; delete them (and the
        # interests) before the contacts they hang off of.
        pg_session.execute(
            delete(Match).where(
                Match.adopter_interest_id.in_(
                    select(AdopterInterest.id).where(
                        AdopterInterest.contact_id.in_(scoped_contact_ids)
                    )
                )
            )
        )
        pg_session.execute(
            delete(AdopterInterest).where(
                AdopterInterest.contact_id.in_(scoped_contact_ids)
            )
        )
        pg_session.execute(
            delete(AdopterInterest).where(
                AdopterInterest.contact_id.in_(test_contact_ids)
            )
        )
        pg_session.execute(
            delete(ContactProfile).where(
                ContactProfile.contact_id.in_(scoped_contact_ids)
            )
        )
        pg_session.execute(
            delete(ContactProfile).where(
                ContactProfile.contact_id.in_(test_contact_ids)
            )
        )
        pg_session.execute(
            delete(Consent).where(Consent.contact_id.in_(test_contact_ids))
        )
        pg_session.execute(
            delete(FacilitatingOrg).where(
                FacilitatingOrg.source_system == "dt",
                FacilitatingOrg.source_id.like("9%"),
            )
        )
        pg_session.execute(delete(Fpg).where(Fpg.people_id3.like("9%")))
        pg_session.execute(
            delete(ActivityLog).where(
                ActivityLog.source_system == "dt",
                ActivityLog.source_id.like("histlog:9%"),
            )
        )
        pg_session.execute(
            delete(ContactAssignment).where(
                ContactAssignment.contact_id.in_(
                    select(Contact.id).where(
                        Contact.source_system.in_(["dt", "forms", "staff_seed"]),
                        Contact.source_id.like("9%"),
                    )
                )
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
            delete(IdentityLink).where(IdentityLink.idp_name == "dt_reconcile")
        )
        pg_session.execute(
            delete(Contact).where(
                Contact.source_id.like("9%"),
                Contact.source_system.in_(["dt", "forms", "staff_seed"]),
            )
        )
        pg_session.execute(
            text(
                "DELETE FROM etl_run WHERE "
                "table_name = 'reconcile_duplicate_email' "
                "AND started_at > now() - interval '5 minutes'"
            )
        )
        # Wipe the one-summary bulk_imported event this track emits so the
        # outbox assertions only ever see THIS test's row.
        pg_session.execute(
            text(
                "DELETE FROM outbox WHERE "
                "event_type = 'jp.adopt.v1.bulk_imported' "
                "AND payload_json->>'label' = 'dt_reconcile:duplicate_email'"
            )
        )
        pg_session.commit()

    _wipe()
    yield
    _wipe()


# ─── mocked DT reader ──────────────────────────────────────────────────────


def _make_dt_reader(payloads: dict[str, dict]):
    """payloads: {source_id: {'post_row': {...}, 'meta_rows': [...]}}."""
    def reader(_conn, source_id: str):
        entry = payloads.get(source_id)
        if entry is None:
            return None, []
        return entry["post_row"], entry["meta_rows"]

    return reader


def _post_row(post_id: int, title: str) -> dict:
    return {
        "ID": post_id,
        "post_title": title,
        "post_status": "publish",
        "post_date": None,
        "post_date_gmt": None,
        "post_modified": None,
        "post_modified_gmt": None,
    }


def _meta(sub_type="adopter", status="new", phone=None, sources=None) -> list[dict]:
    rows = [
        {"meta_key": "sub_type", "meta_value": sub_type},
        {"meta_key": "overall_status", "meta_value": status},
    ]
    if phone is not None:
        # DT comm-channel shape: hashed key under contact_phone_<hex>.
        rows.append({"meta_key": "contact_phone_abc", "meta_value": phone})
    if sources is not None:
        rows.append({"meta_key": "sources", "meta_value": sources})
    return rows


# ─── helpers ───────────────────────────────────────────────────────────────


def _seed_target(pg_session, *, email, name, source_system="forms",
                 source_id="9500", b2c_subject_id=None, phone=None, origin=None):
    c = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name=name,
        email_normalized=email,
        source_system=source_system,
        source_id=source_id,
        b2c_subject_id=b2c_subject_id,
        phone=phone,
        origin=origin,
    )
    pg_session.add(c)
    pg_session.flush()
    return c


def _seed_loser(pg_session, *, source_id, name, phone=None, origin=None):
    """The DT contact whose email was dropped to NULL during import."""
    c = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name=name,
        email_normalized=None,
        source_system="dt",
        source_id=source_id,
        phone=phone,
        origin=origin,
    )
    pg_session.add(c)
    pg_session.flush()
    return c


def _seed_conflict(pg_session, *, source_id, email):
    pg_session.execute(
        text(
            "INSERT INTO migration_conflicts "
            "(id, source_system, source_id, table_name, conflict_type, source_value) "
            "VALUES (:id, 'dt', :sid, 'contacts', 'duplicate_email', "
            "CAST(:sv AS jsonb))"
        ),
        {
            "id": str(uuid.uuid4()),
            "sid": source_id,
            "sv": f'{{"email_normalized": "{email}"}}',
        },
    )
    pg_session.flush()


def _seed_open_match(pg_session, *, contact_id, people_id3="90001", status="recommended"):
    """Seed an FPG + facilitating org + adopter_interest + an OPEN match on
    ``contact_id``. Returns the Match id."""
    pg_session.execute(
        text(
            "INSERT INTO fpg (people_id3, name, frontier) VALUES "
            "(:pid, 'Test FPG 9', true) ON CONFLICT (people_id3) DO NOTHING"
        ),
        {"pid": people_id3},
    )
    org = FacilitatingOrg(
        id=uuid.uuid4(),
        name="Test Org 9",
        source_system="dt",
        source_id="9900",
    )
    pg_session.add(org)
    interest = AdopterInterest(
        id=uuid.uuid4(),
        contact_id=contact_id,
        people_id3=people_id3,
        source_system="local",
        source_id=None,
    )
    pg_session.add(interest)
    pg_session.flush()
    match = Match(
        id=uuid.uuid4(),
        adopter_interest_id=interest.id,
        facilitator_org_id=org.id,
        status=status,
    )
    pg_session.add(match)
    pg_session.flush()
    return match.id


# ─── tests ─────────────────────────────────────────────────────────────────


def test_contact_with_open_match_is_skipped(pg_session):
    email = "openmatch@9test.dev"
    target = _seed_target(pg_session, email=email, name="Jane Doe")
    _seed_loser(pg_session, source_id="9700", name="Jane Doe")
    _seed_conflict(pg_session, source_id="9700", email=email)
    _seed_open_match(pg_session, contact_id=target.id, status="recommended")
    pg_session.commit()

    reader = _make_dt_reader(
        {"9700": {"post_row": _post_row(9700, "Jane Doe"),
                  "meta_rows": _meta(phone="555-7777")}}
    )
    result = reconcile(
        pg_session=pg_session, mysql_conn=object(),
        mode="production", dt_reader=reader, allow_unsafe_merge=True,
    )
    plans = {p.source_id: p for p in result.planned}
    assert plans["9700"].status == "skip_open_match"
    assert plans["9700"] in result.skipped
    assert len(result.to_merge) == 0

    # Conflict left in place — routed to Amy, not merged.
    assert pg_session.execute(
        select(MigrationConflict).where(MigrationConflict.source_id == "9700")
    ).scalars().first() is not None
    # No field overwrite landed.
    pg_session.refresh(target)
    assert target.phone is None


def test_dt_overwrites_nonempty_status_and_fields(pg_session):
    email = "overwrite@9test.dev"
    target = _seed_target(
        pg_session, email=email, name="Jane Doe",
        b2c_subject_id="subj-9-ow", phone="OLD", origin="forms",
    )
    target.adopter_status = "new"
    pg_session.flush()
    _seed_loser(pg_session, source_id="9710", name="Jane Doe")
    _seed_conflict(pg_session, source_id="9710", email=email)
    pg_session.commit()

    # DT carries a newer phone, origin, and a moved-on workflow status.
    # DT 'engaged' maps to adopter_status 'contacted' (status.py).
    reader = _make_dt_reader(
        {"9710": {"post_row": _post_row(9710, "Jane Doe"),
                  "meta_rows": _meta(status="engaged", phone="NEW",
                                     sources="referral")}}
    )
    reconcile(
        pg_session=pg_session, mysql_conn=object(),
        mode="production", dt_reader=reader, allow_unsafe_merge=True,
    )
    pg_session.refresh(target)
    # DT wins on non-empty fields AND on workflow status.
    assert target.phone == "NEW"
    assert target.origin == "referral"
    assert target.adopter_status == "contacted"


def test_dry_run_is_non_mutating_but_writes_etl_run(pg_session):
    email = "merge.me@9test.dev"
    target = _seed_target(pg_session, email=email, name="Jane Doe", phone=None)
    _seed_loser(pg_session, source_id="9601", name="Jane Doe", phone="555-1234")
    _seed_conflict(pg_session, source_id="9601", email=email)
    pg_session.commit()

    reader = _make_dt_reader(
        {"9601": {"post_row": _post_row(9601, "Jane Doe"),
                  "meta_rows": _meta(phone="555-1234")}}
    )

    result = reconcile(
        pg_session=pg_session,
        mysql_conn=object(),
        mode="dry_run",
        dt_reader=reader,
    )

    assert len(result.to_merge) == 1

    # Conflict still present (dry-run did not delete it).
    remaining = pg_session.execute(
        select(MigrationConflict).where(MigrationConflict.source_id == "9601")
    ).scalars().all()
    assert len(remaining) == 1

    # No backfill landed on the target.
    pg_session.refresh(target)
    assert target.phone is None

    # But the etl_run audit row survived the rollback.
    runs = pg_session.execute(
        select(EtlRun).where(
            EtlRun.table_name == "reconcile_duplicate_email",
            EtlRun.mode == "dry_run",
        )
    ).scalars().all()
    assert len(runs) >= 1
    assert runs[-1].rows_in == 1
    assert runs[-1].rows_out_inserted == 0  # dry-run writes nothing

    # No bulk_imported outbox row leaked through the rollback.
    leaked = pg_session.execute(
        text(
            "SELECT count(*) FROM outbox "
            "WHERE event_type = 'jp.adopt.v1.bulk_imported' "
            "AND payload_json->>'label' = 'dt_reconcile:duplicate_email'"
        )
    ).scalar()
    assert leaked == 0


def test_apply_merges_backfills_and_resolves(pg_session):
    email = "real.merge@9test.dev"
    target = _seed_target(
        pg_session, email=email, name="Jane Doe",
        b2c_subject_id="subj-9-jane", phone=None, origin=None,
    )
    loser = _seed_loser(pg_session, source_id="9610", name="Jane Doe")
    _seed_conflict(pg_session, source_id="9610", email=email)
    # Loser has activity history that should move onto the target.
    pg_session.add(
        ActivityLog(
            id=uuid.uuid4(),
            contact_id=loser.id,
            author_id="system:dt_legacy_unknown",
            body="status set to 'new'",
            kind="field_change",
            source_system="dt",
            source_id="histlog:9610001",
            occurred_at=datetime.now(UTC),
        )
    )
    pg_session.commit()

    reader = _make_dt_reader(
        {"9610": {"post_row": _post_row(9610, "Jane Doe"),
                  "meta_rows": _meta(phone="555-9999", sources="referral")}}
    )

    result = reconcile(
        pg_session=pg_session,
        mysql_conn=object(),
        mode="production",
        dt_reader=reader,
        allow_unsafe_merge=True,
    )
    assert len(result.to_merge) == 1

    # Conflict resolved (deleted).
    assert pg_session.execute(
        select(MigrationConflict).where(MigrationConflict.source_id == "9610")
    ).scalars().first() is None

    # Backfill landed (phone + origin were empty on the target).
    pg_session.refresh(target)
    assert target.phone == "555-9999"
    assert target.origin == "referral"

    # Activity history re-pointed onto the target.
    moved = pg_session.execute(
        select(ActivityLog).where(ActivityLog.source_id == "histlog:9610001")
    ).scalars().one()
    assert moved.contact_id == target.id

    # identity_link created for the email -> target subject.
    link = pg_session.execute(
        select(IdentityLink).where(
            IdentityLink.b2c_subject_id == "subj-9-jane"
        )
    ).scalars().first()
    assert link is not None
    assert link.email_normalized == email

    # bulk_imported summary event emitted exactly once.
    n_summary = pg_session.execute(
        text(
            "SELECT count(*) FROM outbox "
            "WHERE event_type = 'jp.adopt.v1.bulk_imported' "
            "AND payload_json->>'label' = 'dt_reconcile:duplicate_email'"
        )
    ).scalar()
    assert n_summary == 1


def test_apply_keeps_local_value_where_dt_is_empty(pg_session):
    # DT-authoritative: DT overwrites where it HAS a value, but a column DT
    # leaves empty keeps the existing core value (no clobber-to-null).
    email = "keep.local@9test.dev"
    target = _seed_target(
        pg_session, email=email, name="Jane Doe",
        b2c_subject_id="subj-9-keep", phone="LOCAL-PHONE", origin="forms",
    )
    _seed_loser(pg_session, source_id="9620", name="Jane Doe")
    _seed_conflict(pg_session, source_id="9620", email=email)
    pg_session.commit()

    # DT has no phone (empty) but a new origin.
    reader = _make_dt_reader(
        {"9620": {"post_row": _post_row(9620, "Jane Doe"),
                  "meta_rows": _meta(sources="referral")}}
    )
    reconcile(
        pg_session=pg_session, mysql_conn=object(),
        mode="production", dt_reader=reader, allow_unsafe_merge=True,
    )
    pg_session.refresh(target)
    # DT empty on phone => core value kept; DT non-empty origin => DT wins.
    assert target.phone == "LOCAL-PHONE"
    assert target.origin == "referral"


def test_ambiguous_name_goes_to_review_not_merged(pg_session, tmp_path):
    email = "family@9test.dev"
    _seed_target(
        pg_session, email=email, name="Bob Jones", b2c_subject_id="subj-9-bob",
    )
    _seed_loser(pg_session, source_id="9630", name="Jane Doe")
    _seed_conflict(pg_session, source_id="9630", email=email)
    pg_session.commit()

    reader = _make_dt_reader(
        {"9630": {"post_row": _post_row(9630, "Jane Doe"),
                  "meta_rows": _meta()}}
    )
    review_out = tmp_path / "review.csv"
    result = reconcile(
        pg_session=pg_session, mysql_conn=object(),
        mode="production", dt_reader=reader, review_path=str(review_out),
        allow_unsafe_merge=True,
    )
    assert len(result.to_review) == 1
    assert len(result.to_merge) == 0

    # Conflict NOT resolved — left for Amy to confirm.
    assert pg_session.execute(
        select(MigrationConflict).where(MigrationConflict.source_id == "9630")
    ).scalars().first() is not None

    # Review file written with the ambiguous row.
    body = review_out.read_text()
    assert "9630" in body
    assert "Jane Doe" in body and "Bob Jones" in body


def test_apply_is_idempotent(pg_session):
    email = "idem@9test.dev"
    target = _seed_target(
        pg_session, email=email, name="Jane Doe", b2c_subject_id="subj-9-idem",
    )
    _seed_loser(pg_session, source_id="9640", name="Jane Doe")
    _seed_conflict(pg_session, source_id="9640", email=email)
    pg_session.commit()

    reader = _make_dt_reader(
        {"9640": {"post_row": _post_row(9640, "Jane Doe"),
                  "meta_rows": _meta(phone="555-0000")}}
    )
    reconcile(pg_session=pg_session, mysql_conn=object(),
              mode="production", dt_reader=reader, allow_unsafe_merge=True)
    # Second apply: conflict is gone, so nothing to do — must not raise.
    result2 = reconcile(pg_session=pg_session, mysql_conn=object(),
                        mode="production", dt_reader=reader,
                        allow_unsafe_merge=True)
    assert len(result2.planned) == 0
    pg_session.refresh(target)
    assert target.phone == "555-0000"


def test_apply_without_override_is_gated(pg_session):
    """--apply (mode='production') WITHOUT allow_unsafe_merge raises the gate
    error — the merge is diagnostics-only pending the DT-authoritative
    redesign — and writes nothing."""
    email = "gated@9test.dev"
    _seed_target(pg_session, email=email, name="Jane Doe", b2c_subject_id="subj-9-gate")
    _seed_loser(pg_session, source_id="9660", name="Jane Doe")
    _seed_conflict(pg_session, source_id="9660", email=email)
    pg_session.commit()

    reader = _make_dt_reader(
        {"9660": {"post_row": _post_row(9660, "Jane Doe"), "meta_rows": _meta()}}
    )
    with pytest.raises(RuntimeError, match="Track A merge --apply is gated"):
        reconcile(
            pg_session=pg_session,
            mysql_conn=object(),
            mode="production",
            dt_reader=reader,
        )

    # The conflict was left untouched (nothing committed before the raise).
    pg_session.rollback()
    assert pg_session.execute(
        select(MigrationConflict).where(MigrationConflict.source_id == "9660")
    ).scalars().first() is not None


def test_missing_target_is_skipped_not_merged(pg_session):
    # Conflict references an email no local contact owns => skip.
    _seed_loser(pg_session, source_id="9650", name="Ghost")
    _seed_conflict(pg_session, source_id="9650", email="nobody@9test.dev")
    pg_session.commit()

    reader = _make_dt_reader(
        {"9650": {"post_row": _post_row(9650, "Ghost"), "meta_rows": _meta()}}
    )
    result = reconcile(pg_session=pg_session, mysql_conn=object(),
                       mode="production", dt_reader=reader,
                       allow_unsafe_merge=True)
    assert len(result.skipped) == 1
    assert len(result.to_merge) == 0
    # Conflict left in place.
    assert pg_session.execute(
        select(MigrationConflict).where(MigrationConflict.source_id == "9650")
    ).scalars().first() is not None
