"""Track B reconcile integration tests against a real Postgres.

No DT MySQL: Track B resolves purely from core data (the conflict carries
the handle; the contact is found locally by source_id) plus the operator
mapping. All test data is scoped to the 9xxx source_id range and wiped
after each test, mirroring test_orchestrator_integration.py.

Skipped when ETL_TEST_DATABASE_URL_DISABLE is set.
"""

from __future__ import annotations

import os
import uuid

import pytest
from jp_adopt_api.models import (
    Contact,
    ContactAssignment,
    IdentityLink,
    MigrationConflict,
    Outbox,
    StaffIdentityLink,
)
from jp_adopt_etl.reconcile.track_b_assignments import (
    SubjectMapping,
    build_plan,
    distinct_handles,
    reconcile,
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
    """Wipe all 9xxx-range rows + recent bulk Outbox rows after each test."""
    yield
    test_contact_ids = select(Contact.id).where(
        Contact.source_system == "dt", Contact.source_id.like("9%")
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
            Contact.source_system == "dt", Contact.source_id.like("9%")
        )
    )
    pg_session.execute(
        delete(StaffIdentityLink).where(
            StaffIdentityLink.source_system == "dt",
            StaffIdentityLink.dt_user_id.like("9%"),
        )
    )
    pg_session.execute(
        delete(IdentityLink).where(IdentityLink.email_normalized.like("recon-test-%"))
    )
    pg_session.execute(
        text(
            "DELETE FROM outbox WHERE event_type IN "
            "('jp.adopt.v1.bulk_imported', 'jp.adopt.v1.assignee_reconciled') "
            "AND created_at > now() - interval '5 minutes'"
        )
    )
    pg_session.commit()


# ─── helpers ────────────────────────────────────────────────────────────────


def _seed_contact(pg_session, *, source_id: str) -> uuid.UUID:
    cid = uuid.uuid4()
    pg_session.execute(
        text(
            "INSERT INTO contacts (id, party_kind, display_name, version, "
            "source_system, source_id, newsletter_opt_in, "
            "local_modified_after_import) "
            "VALUES (:id, 'adopter', :name, 1, 'dt', :sid, false, false)"
        ),
        {"id": cid, "name": f"Contact {source_id}", "sid": source_id},
    )
    return cid


def _seed_staff_link(pg_session, *, dt_user_id: str, email: str) -> None:
    """A StaffIdentityLink with NULL b2c_subject_id (staff not yet signed in)
    — exactly the state that produces assignee_no_subject conflicts."""
    pg_session.execute(
        text(
            "INSERT INTO staff_identity_link (id, dt_user_id, b2c_subject_id, "
            "email, email_normalized, display_name, status, source_system) "
            "VALUES (:id, :dt, NULL, :email, :email, 'Staff', 'unknown', 'dt')"
        ),
        {"id": uuid.uuid4(), "dt": dt_user_id, "email": email},
    )


def _seed_staff_link_with_subject(
    pg_session, *, dt_user_id: str, email: str, subject: str
) -> None:
    """A StaffIdentityLink that ALREADY carries a real b2c_subject_id (staff
    has signed in). A reconcile must NOT clobber this."""
    pg_session.execute(
        text(
            "INSERT INTO staff_identity_link (id, dt_user_id, b2c_subject_id, "
            "email, email_normalized, display_name, status, source_system) "
            "VALUES (:id, :dt, :subject, :email, :email, 'Staff', 'active', 'dt')"
        ),
        {"id": uuid.uuid4(), "dt": dt_user_id, "subject": subject, "email": email},
    )


def _seed_conflict(pg_session, *, source_id: str, handle: str) -> None:
    pg_session.execute(
        text(
            "INSERT INTO migration_conflicts (id, source_system, source_id, "
            "table_name, conflict_type, source_value) "
            "VALUES (:id, 'dt', :sid, 'contact_assignment', "
            "'assignee_no_subject', :sv)"
        ),
        {
            "id": uuid.uuid4(),
            "sid": source_id,
            "sv": '{"assigned_to": "%s"}' % handle,
        },
    )


def _conflicts(pg_session, source_ids):
    return pg_session.execute(
        select(MigrationConflict.source_id).where(
            MigrationConflict.conflict_type == "assignee_no_subject",
            MigrationConflict.source_id.in_(source_ids),
        )
    ).scalars().all()


# ─── diagnostics ──────────────────────────────────────────────────────────


def test_distinct_handles_aggregates_few_handles_many_conflicts(pg_session):
    """The key diagnostic: many conflict rows, few distinct handles."""
    # 5 conflicts: 3 on user-9001, 2 on user-9002.
    for i in range(3):
        _seed_conflict(pg_session, source_id=f"9100{i}", handle="user-9001")
    for i in range(2):
        _seed_conflict(pg_session, source_id=f"9200{i}", handle="user-9002")
    pg_session.commit()

    stats = distinct_handles(pg_session)
    test_stats = [s for s in stats if s.handle in ("user-9001", "user-9002")]
    assert len(test_stats) == 2
    # Most-frequent first.
    assert test_stats[0].handle == "user-9001"
    assert test_stats[0].count == 3
    assert test_stats[0].wp_user_id == "9001"
    assert test_stats[1].handle == "user-9002"
    assert test_stats[1].count == 2


# ─── dry-run is non-mutating ──────────────────────────────────────────────


def test_dry_run_is_non_mutating(pg_session):
    """Default reconcile() (apply=False) computes a plan but writes nothing:
    no staff link backfill, no assignment, no conflict deletion, no outbox."""
    cid = _seed_contact(pg_session, source_id="93001")
    _seed_staff_link(pg_session, dt_user_id="9301", email="recon-test-a@x.dev")
    _seed_conflict(pg_session, source_id="93001", handle="user-9301")
    pg_session.commit()

    mapping = SubjectMapping.from_dict({"user-9301": "subject-9301"})
    plan = reconcile(pg_session, mapping, apply=False)

    assert plan.applied is False
    assert plan.total_conflicts == 1
    assert plan.staff_links_to_set == 1
    assert plan.assignments_to_resolve == 1
    assert plan.conflicts_to_clear == 1

    pg_session.expire_all()
    # Subject NOT backfilled.
    subj = pg_session.execute(
        select(StaffIdentityLink.b2c_subject_id).where(
            StaffIdentityLink.dt_user_id == "9301"
        )
    ).scalar_one()
    assert subj is None
    # No assignment row created.
    assert (
        pg_session.execute(
            select(ContactAssignment).where(ContactAssignment.contact_id == cid)
        ).first()
        is None
    )
    # Conflict still present.
    assert _conflicts(pg_session, ["93001"]) == ["93001"]
    # No reconcile/bulk outbox event.
    assert (
        pg_session.execute(
            text(
                "SELECT count(*) FROM outbox WHERE event_type IN "
                "('jp.adopt.v1.bulk_imported','jp.adopt.v1.assignee_reconciled') "
                "AND created_at > now() - interval '5 minutes'"
            )
        ).scalar_one()
        == 0
    )


# ─── apply path ───────────────────────────────────────────────────────────


def test_apply_backfills_resolves_and_clears(pg_session):
    """apply=True backfills the subject, resolves the assignment, clears the
    conflict, and writes exactly ONE bulk_imported summary event (suppression
    path) plus the reconcile event."""
    cid1 = _seed_contact(pg_session, source_id="94001")
    cid2 = _seed_contact(pg_session, source_id="94002")
    _seed_staff_link(pg_session, dt_user_id="9401", email="recon-test-b@x.dev")
    # Two conflicts on the same handle → one staff-link upsert, two clears.
    _seed_conflict(pg_session, source_id="94001", handle="user-9401")
    _seed_conflict(pg_session, source_id="94002", handle="user-9401")
    pg_session.commit()

    mapping = SubjectMapping.from_dict({"user-9401": "subject-9401"})
    plan = reconcile(pg_session, mapping, apply=True)

    assert plan.applied is True
    assert plan.staff_links_to_set == 1  # deduped across the two conflicts
    assert plan.assignments_to_resolve == 2
    assert plan.conflicts_to_clear == 2

    pg_session.expire_all()
    subj = pg_session.execute(
        select(StaffIdentityLink.b2c_subject_id).where(
            StaffIdentityLink.dt_user_id == "9401"
        )
    ).scalar_one()
    assert subj == "subject-9401"

    for cid in (cid1, cid2):
        row = pg_session.execute(
            select(ContactAssignment).where(ContactAssignment.contact_id == cid)
        ).scalar_one()
        assert row.user_subject_id == "subject-9401"
        assert row.assigned_by == "dt_import"

    assert _conflicts(pg_session, ["94001", "94002"]) == []

    # Exactly ONE bulk_imported summary (outbox suppression), not per-row.
    bulk = pg_session.execute(
        text(
            "SELECT count(*) FROM outbox WHERE "
            "event_type = 'jp.adopt.v1.bulk_imported' "
            "AND created_at > now() - interval '5 minutes'"
        )
    ).scalar_one()
    assert bulk == 1


def test_apply_is_idempotent(pg_session):
    """Re-running apply after a clean run is a no-op (conflicts already gone,
    subject already set, assignment already owned)."""
    cid = _seed_contact(pg_session, source_id="95001")
    _seed_staff_link(pg_session, dt_user_id="9501", email="recon-test-c@x.dev")
    _seed_conflict(pg_session, source_id="95001", handle="user-9501")
    pg_session.commit()

    mapping = SubjectMapping.from_dict({"user-9501": "subject-9501"})
    reconcile(pg_session, mapping, apply=True)
    # Second run: nothing left to clear.
    plan2 = reconcile(pg_session, mapping, apply=True)

    assert plan2.total_conflicts == 0
    assert plan2.conflicts_to_clear == 0

    pg_session.expire_all()
    row = pg_session.execute(
        select(ContactAssignment).where(ContactAssignment.contact_id == cid)
    ).scalar_one()
    assert row.user_subject_id == "subject-9501"


def test_apply_preserves_staff_override(pg_session):
    """A contact already owned by a staff reassignment (assigned_by !=
    'dt_import') is NOT clobbered. The conflict is still cleared because the
    no_subject condition is resolved."""
    cid = _seed_contact(pg_session, source_id="96001")
    _seed_staff_link(pg_session, dt_user_id="9601", email="recon-test-d@x.dev")
    _seed_conflict(pg_session, source_id="96001", handle="user-9601")
    # Pre-existing staff override.
    pg_session.execute(
        text(
            "INSERT INTO contact_assignment (contact_id, user_subject_id, "
            "assigned_by) VALUES (:cid, 'staff-override-subject', 'staff_ui')"
        ),
        {"cid": cid},
    )
    pg_session.commit()

    mapping = SubjectMapping.from_dict({"user-9601": "subject-9601"})
    reconcile(pg_session, mapping, apply=True)

    pg_session.expire_all()
    row = pg_session.execute(
        select(ContactAssignment).where(ContactAssignment.contact_id == cid)
    ).scalar_one()
    # Staff override preserved.
    assert row.user_subject_id == "staff-override-subject"
    # Conflict still cleared (subject now resolvable; condition gone).
    assert _conflicts(pg_session, ["96001"]) == []


def test_apply_does_not_clobber_existing_b2c_subject(pg_session):
    """A staff member who already signed into B2C has a NON-NULL
    b2c_subject_id. A reconcile --apply must NOT overwrite that real identity
    with the operator-mapping subject — the ON CONFLICT guard skips the row."""
    cid = _seed_contact(pg_session, source_id="99101")
    _seed_staff_link_with_subject(
        pg_session,
        dt_user_id="9910",
        email="recon-test-real@x.dev",
        subject="real-b2c-subject",
    )
    _seed_conflict(pg_session, source_id="99101", handle="user-9910")
    pg_session.commit()

    # Operator mapping would point at a DIFFERENT subject.
    mapping = SubjectMapping.from_dict({"user-9910": "mapping-subject-9910"})
    reconcile(pg_session, mapping, apply=True)

    pg_session.expire_all()
    # The real B2C subject is preserved, NOT replaced by the mapping subject.
    subj = pg_session.execute(
        select(StaffIdentityLink.b2c_subject_id).where(
            StaffIdentityLink.dt_user_id == "9910"
        )
    ).scalar_one()
    assert subj == "real-b2c-subject"

    # The assignment is still placed against the existing (real) subject and
    # the conflict is cleared (no_subject condition is resolved).
    row = pg_session.execute(
        select(ContactAssignment).where(ContactAssignment.contact_id == cid)
    ).scalar_one()
    assert row.user_subject_id == "mapping-subject-9910"
    assert _conflicts(pg_session, ["99101"]) == []


def test_unmapped_handle_is_reported_not_cleared(pg_session):
    """A handle absent from the operator mapping is surfaced as unmapped and
    its conflict is left in place."""
    _seed_contact(pg_session, source_id="97001")
    _seed_conflict(pg_session, source_id="97001", handle="user-9701")
    pg_session.commit()

    plan = reconcile(pg_session, SubjectMapping(by_handle={}), apply=True)
    assert "user-9701" in plan.unmapped_handles
    assert _conflicts(pg_session, ["97001"]) == ["97001"]


def test_apply_with_auth_identity_link(pg_session):
    """When the mapping opts into link_auth_identity + email, a general
    identity_link row is upserted idempotently."""
    _seed_contact(pg_session, source_id="98001")
    _seed_staff_link(pg_session, dt_user_id="9801", email="recon-test-e@x.dev")
    _seed_conflict(pg_session, source_id="98001", handle="user-9801")
    pg_session.commit()

    mapping = SubjectMapping.from_dict(
        {
            "user-9801": {
                "subject": "subject-9801",
                "email": "recon-test-auth@x.dev",
                "link_auth_identity": True,
            }
        }
    )
    plan = reconcile(pg_session, mapping, apply=True)
    assert plan.identity_links_to_set == 1

    pg_session.expire_all()
    link = pg_session.execute(
        select(IdentityLink).where(IdentityLink.b2c_subject_id == "subject-9801")
    ).scalar_one()
    assert link.email_normalized == "recon-test-auth@x.dev"
    # cleanup of this identity_link (email prefix doesn't match recon-test-%
    # filter pattern start, ensure removal):
    pg_session.execute(
        delete(IdentityLink).where(IdentityLink.b2c_subject_id == "subject-9801")
    )
    pg_session.commit()


# ─── service-account handles ──────────────────────────────────────────────


def test_service_handle_apply_clears_conflicts_without_assigning(pg_session):
    """A handle marked as a service account (mapping value null) has its
    conflict rows DELETED and creates NO contact_assignment. It is not
    counted as unmapped; the discarded count is surfaced."""
    cid1 = _seed_contact(pg_session, source_id="9A001")
    cid2 = _seed_contact(pg_session, source_id="9A002")
    # Two conflicts on the service handle.
    _seed_conflict(pg_session, source_id="9A001", handle="user-2")
    _seed_conflict(pg_session, source_id="9A002", handle="user-2")
    pg_session.commit()

    mapping = SubjectMapping.from_dict({"user-2": None})
    plan = reconcile(pg_session, mapping, apply=True)

    assert plan.applied is True
    # Not unmapped — intentionally resolved as a service account.
    assert "user-2" not in plan.unmapped_handles
    assert plan.service_handles_cleared == 2
    assert plan.assignments_discarded == 2
    # No real assignment resolution happened for the service handle.
    assert plan.assignments_to_resolve == 0

    pg_session.expire_all()
    # No assignment rows created.
    for cid in (cid1, cid2):
        assert (
            pg_session.execute(
                select(ContactAssignment).where(ContactAssignment.contact_id == cid)
            ).first()
            is None
        )
    # Conflicts deleted.
    assert _conflicts(pg_session, ["9A001", "9A002"]) == []


def test_service_handle_dry_run_writes_nothing(pg_session):
    """Dry-run on a service handle reports the would-be clears but writes
    nothing — the conflicts remain."""
    cid = _seed_contact(pg_session, source_id="9B001")
    _seed_conflict(pg_session, source_id="9B001", handle="user-2")
    pg_session.commit()

    mapping = SubjectMapping.from_dict({"user-2": "__service__"})
    plan = reconcile(pg_session, mapping, apply=False)

    assert plan.applied is False
    assert "user-2" not in plan.unmapped_handles
    assert plan.service_handles_cleared == 1
    assert plan.assignments_discarded == 1

    pg_session.expire_all()
    # Nothing written: conflict still present, no assignment.
    assert _conflicts(pg_session, ["9B001"]) == ["9B001"]
    assert (
        pg_session.execute(
            select(ContactAssignment).where(ContactAssignment.contact_id == cid)
        ).first()
        is None
    )
    # No bulk/reconcile outbox event.
    assert (
        pg_session.execute(
            text(
                "SELECT count(*) FROM outbox WHERE event_type IN "
                "('jp.adopt.v1.bulk_imported','jp.adopt.v1.assignee_reconciled') "
                "AND created_at > now() - interval '5 minutes'"
            )
        ).scalar_one()
        == 0
    )


def test_service_and_real_and_unmapped_handles_coexist(pg_session):
    """A mixed run: a real-subject handle still resolves+assigns; a service
    handle is cleared with no assignment; a truly-unmapped handle stays
    unmapped and its conflict is left in place."""
    cid_real = _seed_contact(pg_session, source_id="9C001")
    cid_svc = _seed_contact(pg_session, source_id="9C002")
    _seed_contact(pg_session, source_id="9C003")
    _seed_staff_link(pg_session, dt_user_id="9510", email="recon-test-mix@x.dev")
    _seed_conflict(pg_session, source_id="9C001", handle="user-9510")
    _seed_conflict(pg_session, source_id="9C002", handle="user-2")
    _seed_conflict(pg_session, source_id="9C003", handle="user-9999")  # unmapped
    pg_session.commit()

    mapping = SubjectMapping.from_dict(
        {"user-9510": "subject-9510", "user-2": None}
    )
    plan = reconcile(pg_session, mapping, apply=True)

    # Real handle resolved + assigned.
    assert plan.assignments_to_resolve == 1
    assert plan.conflicts_to_clear == 1
    # Service handle discarded.
    assert plan.service_handles_cleared == 1
    assert plan.assignments_discarded == 1
    # Truly-unmapped handle stays unmapped, conflict left in place.
    assert "user-9999" in plan.unmapped_handles
    assert "user-2" not in plan.unmapped_handles

    pg_session.expire_all()
    real_row = pg_session.execute(
        select(ContactAssignment).where(ContactAssignment.contact_id == cid_real)
    ).scalar_one()
    assert real_row.user_subject_id == "subject-9510"
    # Service contact: no assignment.
    assert (
        pg_session.execute(
            select(ContactAssignment).where(ContactAssignment.contact_id == cid_svc)
        ).first()
        is None
    )
    # Real + service conflicts cleared; unmapped conflict remains.
    assert _conflicts(pg_session, ["9C001"]) == []
    assert _conflicts(pg_session, ["9C002"]) == []
    assert _conflicts(pg_session, ["9C003"]) == ["9C003"]
