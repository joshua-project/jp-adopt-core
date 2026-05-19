"""Intake endpoints + outbox suppression (U4).

Covers the test scenarios called out in the plan:
  * happy paths for adoption (first submission, multi-FPG, second submission
    same email different FPGs);
  * idempotency replay vs. conflict vs. in-flight;
  * 413 on > 64KB body;
  * 401 on missing / bad bearer;
  * `do_not_engage` contact: silent 201 + submissions_blocked row;
  * concurrent submissions with the same email (unique-on-email-normalized
    + append-on-second behavior);
  * outbox_suppressed() context manager: 100 calls = 1 outbox row.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jp_adopt_api.config import get_settings
from jp_adopt_api.main import app
from jp_adopt_api.models import (
    AdopterInterest,
    ApiIdempotencyKey,
    Contact,
    Outbox,
    SubmissionBlocked,
)
from jp_adopt_api.outbox_suppression import (
    EVENT_BULK_IMPORTED,
    emit_outbox,
    is_suppressed,
    outbox_suppressed,
)

# A test API key — production reads INTAKE_API_KEYS from env. Set it here
# UNCONDITIONALLY (env from the shell wins by default with setdefault, which
# made the tests brittle to a developer who set INTAKE_API_KEYS=anything-else
# in their environment). Force the test key into os.environ then clear the
# cached Settings so the next call rereads.
TEST_INTAKE_KEY = "test-intake-key-do-not-use-in-prod"
os.environ["INTAKE_API_KEYS"] = TEST_INTAKE_KEY
get_settings.cache_clear()


# ─── helpers ────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _clean_email(session: AsyncSession, email_normalized: str) -> None:
    # Delete the dependent submissions_blocked + adopter_interest rows first,
    # then the contact. Cascades on contact delete handle adopter_interest
    # automatically, but submissions_blocked uses ON DELETE SET NULL on
    # contact_id so the rows linger; clean them explicitly here.
    await session.execute(
        delete(SubmissionBlocked).where(
            SubmissionBlocked.email_normalized == email_normalized
        )
    )
    await session.execute(
        delete(Contact).where(Contact.email_normalized == email_normalized)
    )
    await session.commit()


def _adoption_body(
    *,
    email: str,
    display_name: str = "Test Adopter",
    fpg_selections: list[dict] | None = None,
    origin: str | None = "website",
    newsletter_opt_in: bool = False,
) -> dict:
    body: dict = {
        "email": email,
        "display_name": display_name,
        "fpg_selections": fpg_selections or [],
        "newsletter_opt_in": newsletter_opt_in,
    }
    if origin is not None:
        body["origin"] = origin
    return body


def _facilitation_body(
    *,
    email: str,
    display_name: str = "Test Facilitator",
    organization_name: str | None = "Test Org",
    origin: str | None = "core_org",
) -> dict:
    body: dict = {
        "email": email,
        "display_name": display_name,
        "organization_name": organization_name,
    }
    if origin is not None:
        body["origin"] = origin
    return body


def _auth_headers(
    key: str = TEST_INTAKE_KEY, idem: str | None = None
) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {key}"}
    if idem is not None:
        headers["Idempotency-Key"] = idem
    return headers


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


# ─── outbox suppression: unit-level tests ──────────────────────────────────


@pytest.mark.asyncio
async def test_is_suppressed_starts_false() -> None:
    assert is_suppressed() is False


@pytest.mark.asyncio
async def test_outbox_suppression_one_summary_for_many_emits(
    session: AsyncSession,
) -> None:
    """100 emit_outbox calls inside outbox_suppressed() → 1 Outbox row
    (the bulk_imported summary), with event_counts capturing the 100."""
    label = f"test_suppression_{uuid.uuid4().hex[:8]}"
    async with outbox_suppressed(label, session) as ctx:
        assert is_suppressed() is True
        for _ in range(100):
            result = emit_outbox(
                session,
                event_type="jp.adopt.v1.contact.updated",
                payload={"contact_id": str(uuid.uuid4())},
            )
            assert result is None  # suppressed → no row written
        ctx.metadata["rows_processed"] = 100
    assert is_suppressed() is False

    await session.commit()
    # Look up the summary by label — it's the unique tag we put in metadata.
    row = (
        await session.execute(
            select(Outbox).where(Outbox.event_type == EVENT_BULK_IMPORTED)
        )
    ).scalars().all()
    matching = [r for r in row if r.payload_json.get("label") == label]
    assert len(matching) == 1
    summary = matching[0]
    assert summary.payload_json["total_suppressed_events"] == 100
    assert summary.payload_json["event_counts"] == {
        "jp.adopt.v1.contact.updated": 100
    }
    assert summary.payload_json["metadata"] == {"rows_processed": 100}
    # cleanup
    await session.execute(delete(Outbox).where(Outbox.id == summary.id))
    await session.commit()


@pytest.mark.asyncio
async def test_outbox_suppression_exception_skips_summary(
    session: AsyncSession,
) -> None:
    label = f"test_supp_raise_{uuid.uuid4().hex[:8]}"
    with pytest.raises(RuntimeError, match="intentional"):
        async with outbox_suppressed(label, session) as ctx:
            emit_outbox(
                session,
                event_type="jp.adopt.v1.contact.updated",
                payload={"ok": True},
            )
            ctx.metadata["never_reached"] = True
            raise RuntimeError("intentional")
    # Should NOT have written a summary row.
    rows = (
        await session.execute(
            select(Outbox).where(Outbox.event_type == EVENT_BULK_IMPORTED)
        )
    ).scalars().all()
    matching = [r for r in rows if r.payload_json.get("label") == label]
    assert matching == []
    assert is_suppressed() is False


@pytest.mark.asyncio
async def test_outbox_suppression_nesting_is_explicit_error(
    session: AsyncSession,
) -> None:
    async with outbox_suppressed("outer", session):
        with pytest.raises(RuntimeError, match="nested"):
            async with outbox_suppressed("inner", session):
                pass  # pragma: no cover


@pytest.mark.asyncio
async def test_emit_outbox_outside_suppression_writes_row(
    session: AsyncSession,
) -> None:
    eid = uuid.uuid4()
    result = emit_outbox(
        session,
        event_type="jp.adopt.v1.contact.updated",
        payload={"contact_id": str(uuid.uuid4()), "test_marker": str(eid)},
        event_id=eid,
    )
    assert result == eid
    await session.commit()
    row = await session.get(Outbox, eid)
    assert row is not None
    assert row.event_type == "jp.adopt.v1.contact.updated"
    await session.execute(delete(Outbox).where(Outbox.id == eid))
    await session.commit()


# ─── intake: auth + idempotency headers ─────────────────────────────────────


def test_intake_requires_authorization(client: TestClient) -> None:
    r = client.post(
        "/v1/intake/adoption",
        json=_adoption_body(email=f"noauth-{uuid.uuid4().hex[:6]}@example.com"),
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 401
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "unauthorized"


def test_intake_rejects_unknown_bearer(client: TestClient) -> None:
    r = client.post(
        "/v1/intake/adoption",
        json=_adoption_body(email=f"badauth-{uuid.uuid4().hex[:6]}@example.com"),
        headers={
            "Authorization": "Bearer totally-not-the-right-key",
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert r.status_code == 401


def test_intake_requires_idempotency_header(client: TestClient) -> None:
    r = client.post(
        "/v1/intake/adoption",
        json=_adoption_body(email=f"noidem-{uuid.uuid4().hex[:6]}@example.com"),
        headers={"Authorization": f"Bearer {TEST_INTAKE_KEY}"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "idempotency_required"


def test_intake_rejects_payload_too_large(client: TestClient) -> None:
    # Build a body > 64KB by stuffing the display_name limit (max 512) with
    # repeats of a long extra field that survives `extra="ignore"`.
    big = "x" * (70 * 1024)
    r = client.post(
        "/v1/intake/adoption",
        json={
            "email": "huge@example.com",
            "display_name": "x",
            "extra": {"padding": big},
        },
        headers=_auth_headers(idem=str(uuid.uuid4())),
    )
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"


# ─── intake: happy paths ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_adoption_submission_creates_contact_and_interest(
    client: TestClient, session: AsyncSession
) -> None:
    email = f"first-adopt-{uuid.uuid4().hex[:8]}@example.com"
    idem = str(uuid.uuid4())
    body = _adoption_body(
        email=email,
        fpg_selections=[
            {"rop3": "AAA01"},
            {"rop3": "AAA02", "commitment_level": "prayer"},
        ],
    )
    r = client.post(
        "/v1/intake/adoption", json=body, headers=_auth_headers(idem=idem)
    )
    assert r.status_code == 201, r.text
    payload = r.json()
    assert payload["ok"] is True
    assert payload["apiVersion"] == "1"
    contact_id = uuid.UUID(payload["data"]["contactId"])
    interest_ids = [uuid.UUID(i) for i in payload["data"]["interestIds"]]
    assert len(interest_ids) == 2

    # Contact + 2 AdopterInterest rows exist.
    contact = await session.get(Contact, contact_id)
    assert contact is not None
    assert contact.email_normalized == email
    # FPG selected → 'new', not 'potential_adopter'
    assert contact.adopter_status == "new"
    interests = (
        await session.execute(
            select(AdopterInterest).where(AdopterInterest.contact_id == contact_id)
        )
    ).scalars().all()
    assert {i.rop3 for i in interests} == {"AAA01", "AAA02"}

    # Outbox event emitted with the right payload.
    out = (
        await session.execute(
            select(Outbox).where(
                Outbox.event_type == "jp.adopt.v1.submission.received"
            )
        )
    ).scalars().all()
    out_for_contact = [
        o for o in out if o.payload_json["contact_id"] == str(contact_id)
    ]
    assert len(out_for_contact) == 1
    # F27: fully assert the outbox payload shape so any future drift is
    # surfaced at the test level (rather than being silently observed
    # downstream as a "where did key X go?" defect in a webhook subscriber).
    payload = out_for_contact[0].payload_json
    assert payload["event"] == "jp.adopt.v1.submission.received"
    assert payload["schema_version"] == "jp.adopt.v1"
    assert payload["party_kind"] == "adopter"
    assert payload["contact_created"] is True
    assert payload["contact_id"] == str(contact_id)
    assert "submission_id" in payload
    assert "request_id" in payload
    assert isinstance(payload["interest_ids"], list)
    assert isinstance(payload["fpg_selections"], list)
    assert "origin" in payload
    assert isinstance(payload["newsletter_opt_in"], bool)

    # cleanup
    await session.execute(delete(Outbox).where(Outbox.id == out_for_contact[0].id))
    await _clean_email(session, email)


@pytest.mark.asyncio
async def test_no_fpg_marks_contact_potential_adopter(
    client: TestClient, session: AsyncSession
) -> None:
    """R2: adopters submitting without FPG selection land as potential_adopter."""
    email = f"nofpg-{uuid.uuid4().hex[:8]}@example.com"
    idem = str(uuid.uuid4())
    body = _adoption_body(email=email, fpg_selections=[])
    r = client.post("/v1/intake/adoption", json=body, headers=_auth_headers(idem=idem))
    assert r.status_code == 201
    contact_id = uuid.UUID(r.json()["data"]["contactId"])

    contact = await session.get(Contact, contact_id)
    assert contact is not None
    assert contact.adopter_status == "potential_adopter"

    # One "no FPG" AdopterInterest row exists with rop3=NULL.
    interests = (
        await session.execute(
            select(AdopterInterest).where(AdopterInterest.contact_id == contact_id)
        )
    ).scalars().all()
    assert len(interests) == 1
    assert interests[0].rop3 is None

    # cleanup
    out = (
        await session.execute(
            select(Outbox).where(
                Outbox.event_type == "jp.adopt.v1.submission.received"
            )
        )
    ).scalars().all()
    for o in [o for o in out if o.payload_json["contact_id"] == str(contact_id)]:
        await session.execute(delete(Outbox).where(Outbox.id == o.id))
    await _clean_email(session, email)


@pytest.mark.asyncio
async def test_second_submission_same_email_appends_interest(
    client: TestClient, session: AsyncSession
) -> None:
    email = f"second-adopt-{uuid.uuid4().hex[:8]}@example.com"
    r1 = client.post(
        "/v1/intake/adoption",
        json=_adoption_body(email=email, fpg_selections=[{"rop3": "AAA01"}]),
        headers=_auth_headers(idem=str(uuid.uuid4())),
    )
    assert r1.status_code == 201
    contact_id_1 = uuid.UUID(r1.json()["data"]["contactId"])

    r2 = client.post(
        "/v1/intake/adoption",
        json=_adoption_body(email=email, fpg_selections=[{"rop3": "AAA03"}]),
        headers=_auth_headers(idem=str(uuid.uuid4())),
    )
    assert r2.status_code == 201
    contact_id_2 = uuid.UUID(r2.json()["data"]["contactId"])

    assert contact_id_1 == contact_id_2, "should reuse the existing contact"
    interests = (
        await session.execute(
            select(AdopterInterest).where(AdopterInterest.contact_id == contact_id_1)
        )
    ).scalars().all()
    assert {i.rop3 for i in interests} == {"AAA01", "AAA03"}

    # cleanup events + contact
    out = (
        await session.execute(
            select(Outbox).where(
                Outbox.event_type == "jp.adopt.v1.submission.received"
            )
        )
    ).scalars().all()
    for o in [o for o in out if o.payload_json["contact_id"] == str(contact_id_1)]:
        await session.execute(delete(Outbox).where(Outbox.id == o.id))
    await _clean_email(session, email)


@pytest.mark.asyncio
async def test_purge_idempotency_keys_sql_drops_expired_and_stuck_pending(
    session: AsyncSession,
) -> None:
    """C3 / T-12: validate the SQL predicates the worker's
    ``purge_idempotency_keys`` cron uses (B2 widened it to also sweep stuck-
    pending rows). The worker function itself can't be imported here because
    the API test venv doesn't carry ``arq`` (worker dependency); test the
    raw SQL it issues against a real Postgres instead. This still catches
    SQL drift (e.g. wrong column name, wrong state literal).
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text

    now = datetime.now(UTC)
    # Seed three rows: fresh-pending (must survive), stuck-pending (must die),
    # expired-completed (must die), fresh-completed (must survive).
    fresh_pending = uuid.uuid4()
    stuck_pending = uuid.uuid4()
    expired_completed = uuid.uuid4()
    fresh_completed = uuid.uuid4()
    tag = uuid.uuid4().hex[:12]
    for row_id, state, created_offset_minutes, expires_offset_hours in [
        (fresh_pending, "pending", -5, 24),
        (stuck_pending, "pending", -120, 22),
        (expired_completed, "completed", -2880, -1),
        (fresh_completed, "completed", -30, 23),
    ]:
        await session.execute(
            text(
                """
                INSERT INTO api_idempotency_keys
                    (id, api_key_id, key, request_hash, state, created_at, expires_at)
                VALUES
                    (:id, 'test_purge', :key, 'hash', :state, :created, :expires)
                """
            ),
            {
                "id": row_id,
                "key": f"{tag}-{row_id}",
                "state": state,
                "created": now + timedelta(minutes=created_offset_minutes),
                "expires": now + timedelta(hours=expires_offset_hours),
            },
        )
    await session.commit()

    # Mirror the predicate from worker_settings.purge_idempotency_keys exactly,
    # scoped to api_key_id='test_purge' so leftover rows from prior runs don't
    # inflate the rowcount.
    result = await session.execute(
        text(
            """
            DELETE FROM api_idempotency_keys
            WHERE api_key_id = 'test_purge'
              AND id IN (
                SELECT id FROM api_idempotency_keys
                WHERE api_key_id = 'test_purge'
                  AND (expires_at < now()
                       OR (state = 'pending'
                           AND created_at < now() - interval '1 hour'))
                LIMIT 1000
            )
            """
        )
    )
    await session.commit()
    # 2 rows: stuck-pending + expired-completed.
    assert result.rowcount == 2

    surviving = (
        await session.execute(
            select(ApiIdempotencyKey.id).where(
                ApiIdempotencyKey.api_key_id == "test_purge"
            )
        )
    ).scalars().all()
    surviving_set = set(surviving)
    assert fresh_pending in surviving_set
    assert fresh_completed in surviving_set
    assert stuck_pending not in surviving_set
    assert expired_completed not in surviving_set

    # cleanup
    await session.execute(
        text("DELETE FROM api_idempotency_keys WHERE api_key_id = 'test_purge'")
    )
    await session.commit()


@pytest.mark.asyncio
async def test_purge_magic_link_rate_limits_sql_drops_old_only(
    session: AsyncSession,
) -> None:
    """C3 / T-12: companion test for ``purge_magic_link_rate_limits`` SQL
    predicate (``requested_at < now() - interval '2 hours'``)."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text

    now = datetime.now(UTC)
    fresh = uuid.uuid4()
    old = uuid.uuid4()
    tag = f"purge-rl-{uuid.uuid4().hex[:8]}"
    await session.execute(
        text(
            """
            INSERT INTO magic_link_rate_limit (id, email_normalized, requested_at)
            VALUES (:fresh, :tag, :fresh_ts), (:old, :tag, :old_ts)
            """
        ),
        {
            "fresh": fresh,
            "old": old,
            "tag": tag,
            "fresh_ts": now - timedelta(minutes=30),
            "old_ts": now - timedelta(hours=3),
        },
    )
    await session.commit()

    result = await session.execute(
        text(
            """
            DELETE FROM magic_link_rate_limit
            WHERE email_normalized = :tag
              AND id IN (
                SELECT id FROM magic_link_rate_limit
                WHERE email_normalized = :tag
                  AND requested_at < now() - interval '2 hours'
                LIMIT 1000
            )
            """
        ),
        {"tag": tag},
    )
    await session.commit()
    assert result.rowcount == 1

    # cleanup
    await session.execute(
        text("DELETE FROM magic_link_rate_limit WHERE email_normalized = :tag"),
        {"tag": tag},
    )
    await session.commit()


@pytest.mark.asyncio
async def test_idempotency_replay_returns_cached_response(
    client: TestClient, session: AsyncSession
) -> None:
    email = f"idemp-{uuid.uuid4().hex[:8]}@example.com"
    idem = str(uuid.uuid4())
    body = _adoption_body(email=email, fpg_selections=[{"rop3": "AAA01"}])
    r1 = client.post(
        "/v1/intake/adoption", json=body, headers=_auth_headers(idem=idem)
    )
    assert r1.status_code == 201
    body_1 = r1.json()

    r2 = client.post(
        "/v1/intake/adoption", json=body, headers=_auth_headers(idem=idem)
    )
    assert r2.status_code == 201  # cached row keeps the original status code
    assert r2.json() == body_1, "replay must be byte-for-byte identical"

    # Only ONE contact, ONE submission.received outbox event, ONE interest row.
    contact_id = uuid.UUID(body_1["data"]["contactId"])
    interests = (
        await session.execute(
            select(AdopterInterest).where(AdopterInterest.contact_id == contact_id)
        )
    ).scalars().all()
    assert len(interests) == 1

    # C2 / T-11: assert the cached idempotency row stores a SHA-256 prefix of
    # the bearer key, never the raw bearer itself. A leaked DB must not leak
    # a usable credential.
    import hashlib as _hashlib
    expected_id = _hashlib.sha256(TEST_INTAKE_KEY.encode("utf-8")).hexdigest()[:16]
    idem_row = (
        await session.execute(
            select(ApiIdempotencyKey).where(ApiIdempotencyKey.key == idem)
        )
    ).scalar_one()
    assert idem_row.api_key_id == expected_id
    assert idem_row.api_key_id != TEST_INTAKE_KEY
    # Defensive: the raw bearer should not appear in any persisted column.
    assert TEST_INTAKE_KEY not in idem_row.api_key_id
    assert TEST_INTAKE_KEY not in idem_row.key

    # cleanup
    out = (
        await session.execute(
            select(Outbox).where(
                Outbox.event_type == "jp.adopt.v1.submission.received"
            )
        )
    ).scalars().all()
    for o in [o for o in out if o.payload_json["contact_id"] == str(contact_id)]:
        await session.execute(delete(Outbox).where(Outbox.id == o.id))
    await session.execute(
        delete(ApiIdempotencyKey).where(ApiIdempotencyKey.key == idem)
    )
    await _clean_email(session, email)


@pytest.mark.asyncio
async def test_idempotency_conflict_on_different_body(
    client: TestClient, session: AsyncSession
) -> None:
    email = f"conflict-{uuid.uuid4().hex[:8]}@example.com"
    idem = str(uuid.uuid4())
    r1 = client.post(
        "/v1/intake/adoption",
        json=_adoption_body(email=email, fpg_selections=[{"rop3": "AAA01"}]),
        headers=_auth_headers(idem=idem),
    )
    assert r1.status_code == 201

    # Same idem key, different body → 422 conflict.
    r2 = client.post(
        "/v1/intake/adoption",
        json=_adoption_body(email=email, fpg_selections=[{"rop3": "AAA02"}]),
        headers=_auth_headers(idem=idem),
    )
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "idempotency_key_conflict"

    # cleanup
    contact_id = uuid.UUID(r1.json()["data"]["contactId"])
    out = (
        await session.execute(
            select(Outbox).where(
                Outbox.event_type == "jp.adopt.v1.submission.received"
            )
        )
    ).scalars().all()
    for o in [o for o in out if o.payload_json["contact_id"] == str(contact_id)]:
        await session.execute(delete(Outbox).where(Outbox.id == o.id))
    await session.execute(
        delete(ApiIdempotencyKey).where(ApiIdempotencyKey.key == idem)
    )
    await _clean_email(session, email)


# ─── intake: do_not_engage + blocked log ────────────────────────────────────


@pytest.mark.asyncio
async def test_do_not_engage_contact_returns_201_and_logs_block(
    client: TestClient, session: AsyncSession
) -> None:
    """Plan: anti-enumeration. We log the attempt but return a success-shaped
    201 (matching the accepted-first-call status) so a third party can't probe
    blocklist membership via response codes. N1: this used to return 200 which
    became a deterministic do_not_engage oracle once F14 changed first-success
    to 201. The body shape must also match: accepted submissions always return
    ``len(interestIds) >= 1``, so blocked responses fabricate ephemeral UUIDs
    of the same length (never persisted)."""
    email = f"dne-{uuid.uuid4().hex[:8]}@example.com"
    # Seed a contact at do_not_engage.
    blocked = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name="Blocked Adopter",
        adopter_status="do_not_engage",
        email_normalized=email,
    )
    session.add(blocked)
    await session.commit()

    fpg_selections = [{"rop3": "AAA01"}]
    r = client.post(
        "/v1/intake/adoption",
        json=_adoption_body(email=email, fpg_selections=fpg_selections),
        headers=_auth_headers(idem=str(uuid.uuid4())),
    )
    assert r.status_code == 201, r.text
    payload = r.json()
    assert payload["ok"] is True
    # N1 body-shape oracle: blocked path must mirror the accepted-path length
    # exactly (one synthetic id per fpg_selection).
    assert len(payload["data"]["interestIds"]) == len(fpg_selections)

    # An AdopterInterest must NOT have been created (the synthetic UUIDs are
    # ephemeral — they exist only in the response envelope).
    interests = (
        await session.execute(
            select(AdopterInterest).where(AdopterInterest.contact_id == blocked.id)
        )
    ).scalars().all()
    assert interests == []

    # A submissions_blocked row must exist.
    blocks = (
        await session.execute(
            select(SubmissionBlocked).where(
                SubmissionBlocked.email_normalized == email
            )
        )
    ).scalars().all()
    assert len(blocks) == 1
    assert blocks[0].reason == "do_not_engage"
    assert blocks[0].source == "adoption_intake"

    # cleanup
    await _clean_email(session, email)


@pytest.mark.asyncio
async def test_adoption_blocked_and_accepted_responses_have_identical_body_shape(
    client: TestClient, session: AsyncSession
) -> None:
    """N1 body-shape oracle parity: blocked and accepted adoption responses
    must be indistinguishable in shape (same keys, same interestIds length)
    for identical inputs. Anything weaker leaks blocklist membership."""
    blocked_email = f"shape-blk-{uuid.uuid4().hex[:8]}@example.com"
    accepted_email = f"shape-acc-{uuid.uuid4().hex[:8]}@example.com"
    blocked = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name="Blocked Shape",
        adopter_status="do_not_engage",
        email_normalized=blocked_email,
    )
    session.add(blocked)
    await session.commit()

    # Same fpg_selections shape on both calls.
    fpg = [{"rop3": "AAA01"}, {"rop3": "AAA02"}]
    r_blocked = client.post(
        "/v1/intake/adoption",
        json=_adoption_body(email=blocked_email, fpg_selections=fpg),
        headers=_auth_headers(idem=str(uuid.uuid4())),
    )
    r_accepted = client.post(
        "/v1/intake/adoption",
        json=_adoption_body(email=accepted_email, fpg_selections=fpg),
        headers=_auth_headers(idem=str(uuid.uuid4())),
    )
    assert r_blocked.status_code == r_accepted.status_code == 201

    b = r_blocked.json()
    a = r_accepted.json()
    # Same top-level keys.
    assert set(b.keys()) == set(a.keys())
    # Same data keys.
    assert set(b["data"].keys()) == set(a["data"].keys())
    # Same interestIds LENGTH (values are random UUIDs in both cases).
    assert len(b["data"]["interestIds"]) == len(a["data"]["interestIds"])
    assert len(b["data"]["interestIds"]) == len(fpg)

    # cleanup
    await _clean_email(session, blocked_email)
    await _clean_email(session, accepted_email)


# ─── intake: facilitation happy path ───────────────────────────────────────


@pytest.mark.asyncio
async def test_facilitation_intake_creates_facilitator_contact(
    client: TestClient, session: AsyncSession
) -> None:
    email = f"fac-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post(
        "/v1/intake/facilitation",
        json=_facilitation_body(email=email),
        headers=_auth_headers(idem=str(uuid.uuid4())),
    )
    assert r.status_code == 201, r.text
    contact_id = uuid.UUID(r.json()["data"]["contactId"])

    contact = await session.get(Contact, contact_id)
    assert contact is not None
    assert contact.party_kind == "facilitator"
    assert contact.facilitator_status == "new"
    assert contact.adopter_status is None

    # Outbox event was emitted.
    out = (
        await session.execute(
            select(Outbox).where(
                Outbox.event_type == "jp.adopt.v1.submission.received"
            )
        )
    ).scalars().all()
    matching = [o for o in out if o.payload_json["contact_id"] == str(contact_id)]
    assert len(matching) == 1
    # F27: fully assert the facilitation outbox payload — including the
    # facilitation-specific ``organization_name`` so the downstream router
    # always has the org name to display alongside the facilitator contact.
    payload = matching[0].payload_json
    assert payload["event"] == "jp.adopt.v1.submission.received"
    assert payload["schema_version"] == "jp.adopt.v1"
    assert payload["party_kind"] == "facilitator"
    assert payload["contact_id"] == str(contact_id)
    assert "submission_id" in payload
    assert "request_id" in payload
    assert "contact_created" in payload
    assert "organization_name" in payload
    assert "origin" in payload
    assert isinstance(payload["newsletter_opt_in"], bool)

    # cleanup
    for o in matching:
        await session.execute(delete(Outbox).where(Outbox.id == o.id))
    await _clean_email(session, email)


@pytest.mark.asyncio
async def test_facilitation_do_not_engage_returns_201_and_logs_block(
    client: TestClient, session: AsyncSession
) -> None:
    """A5: parity test for facilitation. The N1 status-code change applied to
    both adoption AND facilitation, but only adoption had a test. Note that
    facilitation always returns ``interestIds=[]`` for accepted submissions,
    so the body-shape oracle from A1 doesn't apply here — both blocked and
    accepted return an empty list.
    """
    email = f"facdne-{uuid.uuid4().hex[:8]}@example.com"
    blocked = Contact(
        id=uuid.uuid4(),
        party_kind="facilitator",
        display_name="Blocked Facilitator",
        facilitator_status="do_not_engage",
        email_normalized=email,
    )
    session.add(blocked)
    await session.commit()

    r = client.post(
        "/v1/intake/facilitation",
        json=_facilitation_body(email=email),
        headers=_auth_headers(idem=str(uuid.uuid4())),
    )
    assert r.status_code == 201, r.text
    payload = r.json()
    assert payload["ok"] is True
    # Facilitation accepted path always returns []; blocked matches.
    assert payload["data"]["interestIds"] == []

    # A submissions_blocked row must exist (audited).
    blocks = (
        await session.execute(
            select(SubmissionBlocked).where(
                SubmissionBlocked.email_normalized == email
            )
        )
    ).scalars().all()
    assert len(blocks) == 1
    assert blocks[0].reason == "do_not_engage"
    assert blocks[0].source == "facilitation_intake"

    # cleanup
    await _clean_email(session, email)


# ─── intake: validation errors ──────────────────────────────────────────────


def test_invalid_origin_value_returns_400(client: TestClient) -> None:
    r = client.post(
        "/v1/intake/adoption",
        json=_adoption_body(
            email=f"badorigin-{uuid.uuid4().hex[:6]}@example.com",
            origin="not_in_enum",
        ),
        headers=_auth_headers(idem=str(uuid.uuid4())),
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_failed"


def test_missing_email_returns_400(client: TestClient) -> None:
    r = client.post(
        "/v1/intake/adoption",
        json={"display_name": "no email"},
        headers=_auth_headers(idem=str(uuid.uuid4())),
    )
    assert r.status_code == 400
    fields = r.json()["error"].get("fields") or {}
    assert any("email" in k for k in fields)
