"""Track A duplicate_email reconciliation — integration tests.

Real Postgres (migrations must be applied; project dev DB at
127.0.0.1:5434), MOCKED DT source (never a live MySQL connection). All
fixture data is scoped to the source_id 9xxx range and wiped after each
test, mirroring ``test_orchestrator_integration.py``.
"""

from __future__ import annotations

import json
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


def _seed_loser(
    pg_session,
    *,
    source_id,
    name,
    phone=None,
    origin=None,
    own_children=False,
    own_people_id3=None,
):
    """The DT contact whose email was dropped to NULL during import.

    When ``own_children=True`` the loser is seeded with its own dt-keyed child
    rows in production shape — adopter_interest, contact_profile, consent and
    activity_log — so a test can assert none of them are stranded after the
    merge re-points/unions them onto the target (loser deleted, cascades the
    rest). ``own_people_id3`` (when given) is the FPG the loser's interest
    points at; the caller must seed that FPG row first.
    """
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
    if own_children:
        if own_people_id3 is not None:
            pg_session.add(
                AdopterInterest(
                    id=uuid.uuid4(),
                    contact_id=c.id,
                    people_id3=own_people_id3,
                    source_system="dt",
                    source_id=f"{source_id}:{own_people_id3}",
                )
            )
        pg_session.add(
            ContactProfile(
                id=uuid.uuid4(), contact_id=c.id, entity_size="1",
            )
        )
        pg_session.add(
            Consent(
                id=uuid.uuid4(),
                contact_id=c.id,
                consent_type="email",
                version="1",
                content_hash="b" * 64,
                accepted_at=datetime.now(UTC),
            )
        )
        pg_session.add(
            ActivityLog(
                id=uuid.uuid4(),
                contact_id=c.id,
                author_id="system:dt_legacy_unknown",
                body="loser legacy note",
                kind="field_change",
                source_system="dt",
                source_id=f"histlog:{source_id}900",
                occurred_at=datetime.now(UTC),
            )
        )
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


def _seed_open_match(
    pg_session, *, contact_id, people_id3="90001", status="recommended"
):
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
        mode="production", dt_reader=reader,
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
        mode="production", dt_reader=reader,
    )
    pg_session.refresh(target)
    # DT wins on non-empty fields AND on workflow status.
    assert target.phone == "NEW"
    assert target.origin == "referral"
    assert target.adopter_status == "contacted"


def _meta_with_profile(*, entity_size=None, **kw):
    rows = _meta(**kw)
    if entity_size is not None:
        rows.append({"meta_key": "entity_size", "meta_value": entity_size})
    return rows


def _meta_with_fpg(fpg_json, **kw):
    rows = _meta(**kw)
    rows.append({"meta_key": "fpg_submission_data", "meta_value": fpg_json})
    return rows


def test_interests_unioned(pg_session):
    email = "interests@9test.dev"
    target = _seed_target(pg_session, email=email, name="Jane Doe",
                          b2c_subject_id="subj-9-int")
    # Seed FPGs first (FK target for both the loser's interest and the
    # core interest).
    pg_session.execute(
        text(
            "INSERT INTO fpg (people_id3, name, frontier) VALUES "
            "('90010', 'FPG core', true), ('90011', 'FPG dt', true) "
            "ON CONFLICT (people_id3) DO NOTHING"
        )
    )
    # The DT loser already OWNS its own dt-keyed adopter_interest row for
    # 90011 (source_id '9720:90011'); the merge must re-point it onto the
    # target, not let it cascade-delete when the loser stub is removed.
    _seed_loser(
        pg_session, source_id="9720", name="Jane Doe",
        own_children=True, own_people_id3="90011",
    )
    _seed_conflict(pg_session, source_id="9720", email=email)
    # Existing core interest for people 90010.
    pg_session.add(
        AdopterInterest(
            id=uuid.uuid4(), contact_id=target.id, people_id3="90010",
            source_system="local", source_id=None,
        )
    )
    pg_session.commit()

    # DT postmeta still carries the JSON for both 90010 + 90011 (DT export
    # shape); the union must not double-insert 90011 the loser already owns.
    reader = _make_dt_reader(
        {"9720": {"post_row": _post_row(9720, "Jane Doe"),
                  "meta_rows": _meta_with_fpg(
                      json.dumps([{"peopleId3": "90010"},
                                  {"peopleId3": "90011"}])
                  )}}
    )
    reconcile(pg_session=pg_session, mysql_conn=object(),
              mode="production", dt_reader=reader)

    interests = pg_session.execute(
        select(AdopterInterest).where(AdopterInterest.contact_id == target.id)
    ).scalars().all()
    people = {i.people_id3 for i in interests}
    # Union: the pre-existing core FPG and the DT FPG the loser owned are
    # both on the target.
    assert people == {"90010", "90011"}
    # The loser's dt-keyed interest row is genuinely re-pointed (not lost to
    # cascade and not duplicated) — exactly one row carries '9720:90011'.
    keyed = [i for i in interests if i.source_id == "9720:90011"]
    assert len(keyed) == 1
    # No stranded interest left on the (now-deleted) loser.
    stranded = pg_session.execute(
        select(AdopterInterest).where(AdopterInterest.source_id == "9720:90011")
    ).scalars().all()
    assert {i.contact_id for i in stranded} == {target.id}


def test_profile_overwritten_from_dt(pg_session):
    email = "profile@9test.dev"
    target = _seed_target(pg_session, email=email, name="Jane Doe",
                          b2c_subject_id="subj-9-prof")
    # Existing core profile with a stale entity_size.
    pg_session.add(
        ContactProfile(
            id=uuid.uuid4(), contact_id=target.id, entity_size="1",
        )
    )
    _seed_loser(pg_session, source_id="9730", name="Jane Doe")
    _seed_conflict(pg_session, source_id="9730", email=email)
    pg_session.commit()

    reader = _make_dt_reader(
        {"9730": {"post_row": _post_row(9730, "Jane Doe"),
                  "meta_rows": _meta_with_profile(entity_size="31_100")}}
    )
    reconcile(pg_session=pg_session, mysql_conn=object(),
              mode="production", dt_reader=reader)

    prof = pg_session.execute(
        select(ContactProfile).where(ContactProfile.contact_id == target.id)
    ).scalars().one()
    assert prof.entity_size == "31_100"  # DT overwrote core


def test_core_consent_optout_preserved(pg_session):
    email = "consent@9test.dev"
    target = _seed_target(pg_session, email=email, name="Jane Doe",
                          b2c_subject_id="subj-9-cons")
    # Core has a consent acceptance record that must survive the merge.
    pg_session.add(
        Consent(
            id=uuid.uuid4(), contact_id=target.id, consent_type="email",
            version="1", content_hash="a" * 64, accepted_at=datetime.now(UTC),
        )
    )
    _seed_loser(pg_session, source_id="9740", name="Jane Doe")
    _seed_conflict(pg_session, source_id="9740", email=email)
    pg_session.commit()

    reader = _make_dt_reader(
        {"9740": {"post_row": _post_row(9740, "Jane Doe"),
                  "meta_rows": _meta()}}
    )
    reconcile(pg_session=pg_session, mysql_conn=object(),
              mode="production", dt_reader=reader)

    consents = pg_session.execute(
        select(Consent).where(Consent.contact_id == target.id)
    ).scalars().all()
    # The merge must never clear/contradict the core consent record.
    assert len(consents) == 1
    assert consents[0].consent_type == "email"


def test_dt_assignment_replaces(pg_session):
    email = "assign@9test.dev"
    target = _seed_target(pg_session, email=email, name="Jane Doe",
                          b2c_subject_id="subj-9-asg")
    loser = _seed_loser(pg_session, source_id="9750", name="Jane Doe")
    _seed_conflict(pg_session, source_id="9750", email=email)
    # Loser carries a dt_import assignment; target has none.
    pg_session.add(
        ContactAssignment(
            contact_id=loser.id, user_subject_id="staff-dt-9",
            assigned_by="dt_import",
        )
    )
    pg_session.commit()

    reader = _make_dt_reader(
        {"9750": {"post_row": _post_row(9750, "Jane Doe"),
                  "meta_rows": _meta()}}
    )
    reconcile(pg_session=pg_session, mysql_conn=object(),
              mode="production", dt_reader=reader)

    asg = pg_session.execute(
        select(ContactAssignment).where(
            ContactAssignment.contact_id == target.id
        )
    ).scalars().one()
    assert asg.user_subject_id == "staff-dt-9"


def test_durable_resolution_no_reconflict(pg_session):
    email = "durable@9test.dev"
    target = _seed_target(pg_session, email=email, name="Jane Doe",
                          source_system="forms", source_id="9500",
                          b2c_subject_id="subj-9-dur")
    _seed_loser(pg_session, source_id="9760", name="Jane Doe")
    _seed_conflict(pg_session, source_id="9760", email=email)
    pg_session.commit()

    reader = _make_dt_reader(
        {"9760": {"post_row": _post_row(9760, "Jane Doe"),
                  "meta_rows": _meta()}}
    )
    reconcile(pg_session=pg_session, mysql_conn=object(),
              mode="production", dt_reader=reader)

    pg_session.refresh(target)
    # Target adopted the DT keys so the next sync resolves it by
    # (source_system, source_id) — the update path, never a re-collision.
    assert target.source_system == "dt"
    assert target.source_id == "9760"

    # Exactly one contact holds ('dt', '9760') — the loser stub is gone, so
    # the partial unique index is satisfied and no new conflict can form.
    existing = pg_session.execute(
        select(Contact).where(
            Contact.source_system == "dt", Contact.source_id == "9760"
        )
    ).scalars().all()
    assert len(existing) == 1
    assert existing[0].id == target.id


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
        mode="production", dt_reader=reader,
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
              mode="production", dt_reader=reader)
    # Second apply: conflict is gone, so nothing to do — must not raise.
    result2 = reconcile(pg_session=pg_session, mysql_conn=object(),
                        mode="production", dt_reader=reader)
    assert len(result2.planned) == 0
    pg_session.refresh(target)
    assert target.phone == "555-0000"


def test_apply_runs_without_override(pg_session):
    """--apply (mode='production') runs the DT-authoritative merge with no
    override flag — the gate has been removed now the merge is designed."""
    email = "ungated@9test.dev"
    _seed_target(pg_session, email=email, name="Jane Doe", b2c_subject_id="subj-9-ung")
    _seed_loser(pg_session, source_id="9660", name="Jane Doe")
    _seed_conflict(pg_session, source_id="9660", email=email)
    pg_session.commit()

    reader = _make_dt_reader(
        {"9660": {"post_row": _post_row(9660, "Jane Doe"), "meta_rows": _meta()}}
    )
    result = reconcile(
        pg_session=pg_session,
        mysql_conn=object(),
        mode="production",
        dt_reader=reader,
    )
    assert len(result.to_merge) == 1  # merged, no RuntimeError

    # The conflict was resolved (deleted) by the merge.
    assert pg_session.execute(
        select(MigrationConflict).where(MigrationConflict.source_id == "9660")
    ).scalars().first() is None


def test_missing_target_is_skipped_not_merged(pg_session):
    # Conflict references an email no local contact owns => skip.
    _seed_loser(pg_session, source_id="9650", name="Ghost")
    _seed_conflict(pg_session, source_id="9650", email="nobody@9test.dev")
    pg_session.commit()

    reader = _make_dt_reader(
        {"9650": {"post_row": _post_row(9650, "Ghost"), "meta_rows": _meta()}}
    )
    result = reconcile(pg_session=pg_session, mysql_conn=object(),
                       mode="production", dt_reader=reader)
    assert len(result.skipped) == 1
    assert len(result.to_merge) == 0
    # Conflict left in place.
    assert pg_session.execute(
        select(MigrationConflict).where(MigrationConflict.source_id == "9650")
    ).scalars().first() is not None


# ─── fix #1: FPG FK violation — interests with a missing FPG are skipped ──────


def test_interest_with_missing_fpg_is_skipped_not_fk_error(pg_session):
    """A DT interest whose people_id3 is absent from ``fpg`` must NOT be
    inserted (it would violate the adopter_interest.people_id3 FK and abort
    the whole apply). Mirror the importer: load existing fpg ids once, skip
    the unknown one. Merge does not raise; the unknown interest is skipped."""
    email = "missingfpg@9test.dev"
    target = _seed_target(pg_session, email=email, name="Jane Doe",
                          b2c_subject_id="subj-9-mfpg")
    pg_session.execute(
        text(
            "INSERT INTO fpg (people_id3, name, frontier) VALUES "
            "('90020', 'FPG present', true) ON CONFLICT (people_id3) DO NOTHING"
        )
    )
    _seed_loser(pg_session, source_id="9770", name="Jane Doe")
    _seed_conflict(pg_session, source_id="9770", email=email)
    pg_session.commit()

    # DT carries 90020 (present in fpg) AND 90099 (ABSENT — would FK-fail).
    reader = _make_dt_reader(
        {"9770": {"post_row": _post_row(9770, "Jane Doe"),
                  "meta_rows": _meta_with_fpg(
                      json.dumps([{"peopleId3": "90020"},
                                  {"peopleId3": "90099"}])
                  )}}
    )
    # Must not raise an IntegrityError.
    reconcile(pg_session=pg_session, mysql_conn=object(),
              mode="production", dt_reader=reader)

    people = {
        i.people_id3
        for i in pg_session.execute(
            select(AdopterInterest).where(AdopterInterest.contact_id == target.id)
        ).scalars().all()
    }
    # Only the known FPG landed; the unknown one was skipped, not inserted.
    assert people == {"90020"}


# ─── fix #3: protected contacts (do_not_engage / locally-edited) ─────────────


def test_core_do_not_engage_is_protected_and_flagged(pg_session, tmp_path):
    """A core contact whose adopter_status is do_not_engage is PROTECTED: the
    DT merge never overwrites it, the status is unchanged, the conflict stays,
    and it lands on Amy's review list with disposition 'skip_protected'."""
    email = "dne@9test.dev"
    target = _seed_target(pg_session, email=email, name="Jane Doe",
                          b2c_subject_id="subj-9-dne", phone=None)
    target.adopter_status = "do_not_engage"
    pg_session.flush()
    _seed_loser(pg_session, source_id="9780", name="Jane Doe")
    _seed_conflict(pg_session, source_id="9780", email=email)
    pg_session.commit()

    # DT is engaged with a phone — would normally overwrite.
    reader = _make_dt_reader(
        {"9780": {"post_row": _post_row(9780, "Jane Doe"),
                  "meta_rows": _meta(status="engaged", phone="NEW")}}
    )
    review_out = tmp_path / "review.csv"
    result = reconcile(pg_session=pg_session, mysql_conn=object(),
                       mode="production", dt_reader=reader,
                       review_path=str(review_out))

    plans = {p.source_id: p for p in result.planned}
    assert plans["9780"].status == "skip_protected"
    assert len(result.to_merge) == 0

    pg_session.refresh(target)
    # Status + fields untouched.
    assert target.adopter_status == "do_not_engage"
    assert target.phone is None
    # Conflict left for Amy.
    assert pg_session.execute(
        select(MigrationConflict).where(MigrationConflict.source_id == "9780")
    ).scalars().first() is not None
    # On the review list.
    body = review_out.read_text()
    assert "9780" in body
    assert "skip_protected" in body


def test_local_modified_after_import_is_protected_and_flagged(pg_session):
    """A contact a staff member edited in core (local_modified_after_import)
    is PROTECTED exactly as the ETL importer guards it — skipped + flagged,
    never overwritten."""
    email = "edited@9test.dev"
    target = _seed_target(pg_session, email=email, name="Jane Doe",
                          b2c_subject_id="subj-9-edit", phone=None)
    target.local_modified_after_import = True
    pg_session.flush()
    _seed_loser(pg_session, source_id="9790", name="Jane Doe")
    _seed_conflict(pg_session, source_id="9790", email=email)
    pg_session.commit()

    reader = _make_dt_reader(
        {"9790": {"post_row": _post_row(9790, "Jane Doe"),
                  "meta_rows": _meta(status="engaged", phone="NEW")}}
    )
    result = reconcile(pg_session=pg_session, mysql_conn=object(),
                       mode="production", dt_reader=reader)

    plans = {p.source_id: p for p in result.planned}
    assert plans["9790"].status == "skip_protected"
    assert len(result.to_merge) == 0
    assert plans["9790"] in result.for_review_list

    pg_session.refresh(target)
    assert target.phone is None
    # Conflict left for Amy.
    assert pg_session.execute(
        select(MigrationConflict).where(MigrationConflict.source_id == "9790")
    ).scalars().first() is not None


# ─── fix #4: per-contact SAVEPOINT — one failure doesn't poison the batch ─────


def test_one_plan_failure_does_not_abort_other_merges(pg_session, monkeypatch):
    """Each _apply_one runs in its own SAVEPOINT. A failure on one plan rolls
    back only that contact (its conflict row survives) and the other plans
    still apply + resolve."""
    from jp_adopt_etl.reconcile import track_a_duplicate_email as mod

    good_email = "good@9test.dev"
    bad_email = "bad@9test.dev"
    good = _seed_target(pg_session, email=good_email, name="Good Person",
                        b2c_subject_id="subj-9-good", phone=None)
    _seed_target(pg_session, email=bad_email, name="Bad Person",
                 source_id="9501", b2c_subject_id="subj-9-bad")
    _seed_loser(pg_session, source_id="9810", name="Good Person")
    _seed_loser(pg_session, source_id="9811", name="Bad Person")
    _seed_conflict(pg_session, source_id="9810", email=good_email)
    _seed_conflict(pg_session, source_id="9811", email=bad_email)
    pg_session.commit()

    reader = _make_dt_reader({
        "9810": {"post_row": _post_row(9810, "Good Person"),
                 "meta_rows": _meta(phone="555-GOOD")},
        "9811": {"post_row": _post_row(9811, "Bad Person"),
                 "meta_rows": _meta(phone="555-BAD")},
    })

    real_overwrite = mod._apply_overwrite

    def flaky_overwrite(sess, plan):
        if plan.source_id == "9811":
            raise RuntimeError("boom on 9811")
        return real_overwrite(sess, plan)

    monkeypatch.setattr(mod, "_apply_overwrite", flaky_overwrite)

    result = reconcile(pg_session=pg_session, mysql_conn=object(),
                       mode="production", dt_reader=reader)

    plans = {p.source_id: p for p in result.planned}
    # The good plan applied + resolved.
    pg_session.refresh(good)
    assert good.phone == "555-GOOD"
    assert pg_session.execute(
        select(MigrationConflict).where(MigrationConflict.source_id == "9810")
    ).scalars().first() is None
    # The bad plan rolled back: marked failed and its conflict row survives.
    assert plans["9811"].status == "failed"
    assert pg_session.execute(
        select(MigrationConflict).where(MigrationConflict.source_id == "9811")
    ).scalars().first() is not None


# ─── fix #5: multi-collision emails route to Amy, never auto-merge ───────────


def test_two_conflicts_sharing_a_target_go_to_review(pg_session, tmp_path):
    """When >1 duplicate_email conflict resolves to the SAME target contact,
    durable key adoption can't represent many-DT-posts->one-contact, so ALL
    of them route to Amy (disposition 'skip_multi_collision') — neither
    auto-merges."""
    email = "shared@9test.dev"
    _seed_target(pg_session, email=email, name="Jane Doe",
                 b2c_subject_id="subj-9-multi")
    _seed_loser(pg_session, source_id="9820", name="Jane Doe")
    _seed_loser(pg_session, source_id="9821", name="Jane Doe")
    _seed_conflict(pg_session, source_id="9820", email=email)
    _seed_conflict(pg_session, source_id="9821", email=email)
    pg_session.commit()

    reader = _make_dt_reader({
        "9820": {"post_row": _post_row(9820, "Jane Doe"), "meta_rows": _meta()},
        "9821": {"post_row": _post_row(9821, "Jane Doe"), "meta_rows": _meta()},
    })
    review_out = tmp_path / "review.csv"
    result = reconcile(pg_session=pg_session, mysql_conn=object(),
                       mode="production", dt_reader=reader,
                       review_path=str(review_out))

    assert len(result.to_merge) == 0
    plans = {p.source_id: p for p in result.planned}
    assert plans["9820"].status == "skip_multi_collision"
    assert plans["9821"].status == "skip_multi_collision"
    # Both conflicts left in place for Amy.
    for sid in ("9820", "9821"):
        assert pg_session.execute(
            select(MigrationConflict).where(MigrationConflict.source_id == sid)
        ).scalars().first() is not None
    # Both on the review list.
    body = review_out.read_text()
    assert "9820" in body and "9821" in body
    assert "skip_multi_collision" in body


# ─── fix #6: ContactAssignment DT-replace when target already assigned ───────


def test_dt_assignment_replaces_existing_target_assignment(pg_session):
    """Per the spec, ContactAssignment is DT-authoritative replace for clean
    merges: when the target already has a (non-staff) assignment, the DT one
    replaces it."""
    email = "replace-asg@9test.dev"
    target = _seed_target(pg_session, email=email, name="Jane Doe",
                          b2c_subject_id="subj-9-rasg")
    loser = _seed_loser(pg_session, source_id="9830", name="Jane Doe")
    _seed_conflict(pg_session, source_id="9830", email=email)
    # Target ALREADY has a dt_import assignment to a different staffer.
    pg_session.add(
        ContactAssignment(
            contact_id=target.id, user_subject_id="staff-old-9",
            assigned_by="dt_import",
        )
    )
    pg_session.add(
        ContactAssignment(
            contact_id=loser.id, user_subject_id="staff-new-9",
            assigned_by="dt_import",
        )
    )
    pg_session.commit()

    reader = _make_dt_reader(
        {"9830": {"post_row": _post_row(9830, "Jane Doe"), "meta_rows": _meta()}}
    )
    reconcile(pg_session=pg_session, mysql_conn=object(),
              mode="production", dt_reader=reader)

    asg = pg_session.execute(
        select(ContactAssignment).where(
            ContactAssignment.contact_id == target.id
        )
    ).scalars().one()
    # DT's assignment replaced the target's prior dt_import one.
    assert asg.user_subject_id == "staff-new-9"


# ─── fix #8: ContactProfile upsert drops None keys, keeping core values ──────


def test_profile_upsert_keeps_core_value_when_dt_field_is_none(pg_session):
    """An unmappable/clamped DT enum comes through as None; the upsert must
    drop None-valued keys so the core value is kept, not nulled."""
    email = "profnone@9test.dev"
    target = _seed_target(pg_session, email=email, name="Jane Doe",
                          b2c_subject_id="subj-9-pnone")
    # Core profile has a good entity_size that must SURVIVE.
    pg_session.add(
        ContactProfile(
            id=uuid.uuid4(), contact_id=target.id, entity_size="31_100",
        )
    )
    _seed_loser(pg_session, source_id="9840", name="Jane Doe")
    _seed_conflict(pg_session, source_id="9840", email=email)
    pg_session.commit()

    # DT meta has NO entity_size => mapper yields entity_size=None. The merge
    # must not null the core value.
    reader = _make_dt_reader(
        {"9840": {"post_row": _post_row(9840, "Jane Doe"),
                  "meta_rows": _meta()}}
    )
    reconcile(pg_session=pg_session, mysql_conn=object(),
              mode="production", dt_reader=reader)

    prof = pg_session.execute(
        select(ContactProfile).where(ContactProfile.contact_id == target.id)
    ).scalars().one()
    assert prof.entity_size == "31_100"  # core value kept, not nulled


# ─── fix #9: loser's own dt-keyed children are not stranded post-merge ───────


def test_loser_children_not_stranded_after_merge(pg_session):
    """The loser seeded in production shape (its own dt-keyed interest +
    profile + consent + activity) must leave nothing stranded: interest +
    activity re-pointed onto the target, consent/profile cascade-cleaned with
    the deleted loser stub."""
    email = "nostrand@9test.dev"
    target = _seed_target(pg_session, email=email, name="Jane Doe",
                          b2c_subject_id="subj-9-nostrand")
    pg_session.execute(
        text(
            "INSERT INTO fpg (people_id3, name, frontier) VALUES "
            "('90030', 'FPG strand', true) ON CONFLICT (people_id3) DO NOTHING"
        )
    )
    loser = _seed_loser(
        pg_session, source_id="9850", name="Jane Doe",
        own_children=True, own_people_id3="90030",
    )
    loser_id = loser.id
    _seed_conflict(pg_session, source_id="9850", email=email)
    pg_session.commit()

    reader = _make_dt_reader(
        {"9850": {"post_row": _post_row(9850, "Jane Doe"), "meta_rows": _meta()}}
    )
    reconcile(pg_session=pg_session, mysql_conn=object(),
              mode="production", dt_reader=reader)

    # Loser stub deleted.
    assert pg_session.get(Contact, loser_id) is None
    # Its interest + activity re-pointed onto the target.
    interest = pg_session.execute(
        select(AdopterInterest).where(AdopterInterest.source_id == "9850:90030")
    ).scalars().one()
    assert interest.contact_id == target.id
    activity = pg_session.execute(
        select(ActivityLog).where(ActivityLog.source_id == "histlog:9850900")
    ).scalars().one()
    assert activity.contact_id == target.id
    # No child rows still hang off the (deleted) loser.
    for model in (AdopterInterest, ContactProfile, Consent, ContactAssignment):
        stranded = pg_session.execute(
            select(model).where(model.contact_id == loser_id)
        ).scalars().all()
        assert stranded == []
