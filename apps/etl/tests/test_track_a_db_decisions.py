"""``load_decisions_from_db`` — the UI-driven decision source for Track A.

Real Postgres (project dev DB at 127.0.0.1:5434). Fixture rows are scoped to
the ``dr-97xx`` source_id range / ``@9test.dev`` emails and wiped per test.

The cluster-awareness is the crux: a single-collision merge becomes a
``force_merge`` email; a shared-email cluster merge becomes a ``multi_keep``
keeper (and must NOT land in force_merge, or every cluster member would merge).
"""

from __future__ import annotations

import os
import uuid

import pytest
from jp_adopt_api.models import DuplicateReviewDecision, MigrationConflict
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker

from jp_adopt_etl.reconcile.track_a_duplicate_email import load_decisions_from_db

ETL_TEST_DATABASE_URL = os.environ.get(
    "ETL_TEST_DATABASE_URL",
    "postgresql+psycopg2://jp_adopt:jp_adopt@127.0.0.1:5434/jp_adopt",
)

pytestmark = pytest.mark.skipif(
    "ETL_TEST_DATABASE_URL_DISABLE" in os.environ,
    reason="Postgres not available in this environment",
)

_SINGLE_EMAIL = "single@9test.dev"
_SHARED_EMAIL = "shared@9test.dev"
_IDS = ["dr-9701", "dr-9702", "dr-9710"]
_EMAILS = [_SINGLE_EMAIL, _SHARED_EMAIL]


@pytest.fixture
def pg_session():
    engine = create_engine(ETL_TEST_DATABASE_URL, future=True)
    Session = sessionmaker(engine, expire_on_commit=False)
    with Session() as s:
        yield s
    engine.dispose()


@pytest.fixture(autouse=True)
def _cleanup(pg_session):
    def _wipe():
        pg_session.execute(
            delete(MigrationConflict).where(
                MigrationConflict.source_id.in_(_IDS)
            )
        )
        pg_session.execute(
            delete(DuplicateReviewDecision).where(
                DuplicateReviewDecision.email_normalized.in_(_EMAILS)
            )
        )
        pg_session.commit()

    _wipe()
    yield
    _wipe()


def _conflict(source_id: str, email: str) -> MigrationConflict:
    return MigrationConflict(
        id=uuid.uuid4(),
        source_system="dt",
        source_id=source_id,
        table_name="contacts",
        conflict_type="duplicate_email",
        source_value={"email_normalized": email},
    )


def _decision(source_id: str, email: str) -> DuplicateReviewDecision:
    return DuplicateReviewDecision(
        id=uuid.uuid4(),
        email_normalized=email,
        dt_source_id=source_id,
        decision="merge",
    )


def test_empty_table_yields_empty_decisions(pg_session):
    decisions = load_decisions_from_db(pg_session)
    assert decisions.force_merge == set()
    assert decisions.multi_keep == {}


def test_single_collision_merge_becomes_force_merge(pg_session):
    pg_session.add(_conflict("dr-9710", _SINGLE_EMAIL))
    pg_session.add(_decision("dr-9710", _SINGLE_EMAIL))
    pg_session.commit()

    decisions = load_decisions_from_db(pg_session)
    assert _SINGLE_EMAIL in decisions.force_merge
    assert _SINGLE_EMAIL not in decisions.multi_keep


def test_shared_cluster_merge_becomes_multi_keep_only(pg_session):
    # Two DT records collide on one inbox; reviewer picks 9701 as keeper.
    pg_session.add(_conflict("dr-9701", _SHARED_EMAIL))
    pg_session.add(_conflict("dr-9702", _SHARED_EMAIL))
    pg_session.add(_decision("dr-9701", _SHARED_EMAIL))
    pg_session.commit()

    decisions = load_decisions_from_db(pg_session)
    # Cluster size > 1 ⇒ keeper goes to multi_keep, NOT force_merge (which
    # would merge every member onto the owner).
    assert decisions.multi_keep == {_SHARED_EMAIL: "dr-9701"}
    assert _SHARED_EMAIL not in decisions.force_merge
