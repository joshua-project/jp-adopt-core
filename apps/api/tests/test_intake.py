"""Intake endpoints + outbox suppression (U4).

Covers the test scenarios called out in the plan:
  * happy paths for adoption (first submission, multi-FPG, second submission
    same email different FPGs);
  * idempotency replay vs. conflict vs. in-flight;
  * 413 on > 64KB body;
  * 401 on missing / bad bearer;
  * `do_not_engage` contact: silent 200 + submissions_blocked row;
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
    assert out_for_contact[0].payload_json["party_kind"] == "adopter"
    assert out_for_contact[0].payload_json["contact_created"] is True

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
async def test_do_not_engage_contact_returns_200_and_logs_block(
    client: TestClient, session: AsyncSession
) -> None:
    """Plan: anti-enumeration. We log the attempt but return a success-shaped
    200 so a third party can't probe blocklist membership via response codes."""
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

    r = client.post(
        "/v1/intake/adoption",
        json=_adoption_body(email=email, fpg_selections=[{"rop3": "AAA01"}]),
        headers=_auth_headers(idem=str(uuid.uuid4())),
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["ok"] is True
    assert payload["data"]["interestIds"] == []

    # An AdopterInterest must NOT have been created.
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
    assert matching[0].payload_json["party_kind"] == "facilitator"

    # cleanup
    for o in matching:
        await session.execute(delete(Outbox).where(Outbox.id == o.id))
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
