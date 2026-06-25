"""Direct tests for process_*_payload helpers (forms-etl refactor, U1)."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jp_adopt_api.config import get_settings
from jp_adopt_api.domain.state_machine import EVENT_CONTACT_SUBMITTED
from jp_adopt_api.models import Contact, Outbox, SubmissionBlocked
from jp_adopt_api.routers.intake import (
    SOURCE_SYSTEM_FORMS,
    IntakeValidationError,
    process_adoption_payload,
    process_facilitation_payload,
)
from jp_adopt_api.schemas import AdoptionIntake, FacilitationIntake

os.environ.setdefault("INTAKE_API_KEYS", "test-intake-key-do-not-use-in-prod")
get_settings.cache_clear()


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _clean_email(session: AsyncSession, email: str) -> None:
    normalized = email.strip().lower()
    await session.execute(
        delete(SubmissionBlocked).where(
            SubmissionBlocked.email_normalized == normalized
        )
    )
    await session.execute(
        delete(Contact).where(Contact.email_normalized == normalized)
    )
    await session.commit()


@pytest.mark.asyncio
async def test_process_adoption_payload_happy_path(session: AsyncSession) -> None:
    email = f"helper-adopt-{uuid.uuid4().hex[:8]}@example.com"
    await _clean_email(session, email)
    settings = get_settings()
    payload = AdoptionIntake.model_validate(
        {
            "email": email,
            "display_name": "Helper Adopter",
            "fpg_selections": [{"people_id3": "AAA01"}],
        }
    )
    outcome = await process_adoption_payload(session, payload=payload, settings=settings)
    assert outcome.was_blocked is False
    assert outcome.contact_id
    assert len(outcome.interest_ids) == 1
    contact = (
        await session.execute(select(Contact).where(Contact.id == outcome.contact_id))
    ).scalar_one()
    assert contact.email_normalized == email.lower()
    await session.rollback()


@pytest.mark.asyncio
async def test_process_facilitation_payload_happy_path(session: AsyncSession) -> None:
    email = f"helper-fac-{uuid.uuid4().hex[:8]}@example.com"
    await _clean_email(session, email)
    settings = get_settings()
    payload = FacilitationIntake.model_validate(
        {
            "email": email,
            "display_name": "Helper Org",
            "organization_name": "Helper Org",
            "fpg_selections": [
                {
                    "people_id3": "AAA01",
                    "engagement_status": "ready",
                    "facilitation_services": ["prayer"],
                }
            ],
        }
    )
    outcome = await process_facilitation_payload(
        session, payload=payload, settings=settings
    )
    assert outcome.was_blocked is False
    assert len(outcome.interest_ids) == 1
    await session.rollback()


@pytest.mark.asyncio
async def test_process_adoption_payload_blocked(session: AsyncSession) -> None:
    email = f"helper-blocked-{uuid.uuid4().hex[:8]}@example.com"
    await _clean_email(session, email)
    settings = get_settings()
    contact = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name="Blocked",
        adopter_status="do_not_engage",
        email_normalized=email.lower(),
    )
    session.add(contact)
    await session.commit()

    payload = AdoptionIntake.model_validate(
        {
            "email": email,
            "display_name": "Blocked Try",
            "fpg_selections": [{"people_id3": "AAA01"}],
        }
    )
    outcome = await process_adoption_payload(session, payload=payload, settings=settings)
    assert outcome.was_blocked is True
    blocked = (
        await session.execute(
            select(SubmissionBlocked).where(
                SubmissionBlocked.email_normalized == email.lower()
            )
        )
    ).scalar_one()
    assert blocked.reason == "do_not_engage"
    await session.rollback()


@pytest.mark.asyncio
async def test_override_created_at_on_insert(session: AsyncSession) -> None:
    email = f"helper-ts-{uuid.uuid4().hex[:8]}@example.com"
    await _clean_email(session, email)
    settings = get_settings()
    historical = datetime(2024, 11, 15, 10, 0, 0, tzinfo=UTC)
    payload = AdoptionIntake.model_validate(
        {
            "email": email,
            "display_name": "Historical",
            "fpg_selections": [],
        }
    )
    outcome = await process_adoption_payload(
        session,
        payload=payload,
        settings=settings,
        override_created_at=historical,
        source_system=SOURCE_SYSTEM_FORMS,
        source_id=str(uuid.uuid4()),
    )
    contact = (
        await session.execute(select(Contact).where(Contact.id == outcome.contact_id))
    ).scalar_one()
    assert contact.created_at == historical
    assert contact.source_system == SOURCE_SYSTEM_FORMS
    await session.rollback()


@pytest.mark.asyncio
async def test_override_created_at_ignored_on_existing(session: AsyncSession) -> None:
    email = f"helper-existing-{uuid.uuid4().hex[:8]}@example.com"
    await _clean_email(session, email)
    settings = get_settings()
    original = datetime(2023, 1, 1, 12, 0, 0, tzinfo=UTC)
    contact = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name="Existing",
        adopter_status="new",
        email_normalized=email.lower(),
        created_at=original,
    )
    session.add(contact)
    await session.commit()

    payload = AdoptionIntake.model_validate(
        {
            "email": email,
            "display_name": "Existing Again",
            "fpg_selections": [{"people_id3": "AAA01"}],
        }
    )
    await process_adoption_payload(
        session,
        payload=payload,
        settings=settings,
        override_created_at=datetime(2024, 6, 1, tzinfo=UTC),
    )
    refreshed = (
        await session.execute(select(Contact).where(Contact.id == contact.id))
    ).scalar_one()
    assert refreshed.created_at == original
    await session.rollback()


@pytest.mark.asyncio
async def test_process_adoption_unknown_people_id3_raises(session: AsyncSession) -> None:
    email = f"helper-badpg-{uuid.uuid4().hex[:8]}@example.com"
    payload = AdoptionIntake.model_validate(
        {
            "email": email,
            "display_name": "Bad PG",
            "fpg_selections": [{"people_id3": "DOES_NOT_EXIST"}],
        }
    )
    with pytest.raises(IntakeValidationError):
        await process_adoption_payload(
            session, payload=payload, settings=get_settings()
        )


@pytest.mark.parametrize("model", [AdoptionIntake, FacilitationIntake])
def test_fpg_selections_cap_accepts_2000_rejects_2001(model) -> None:
    """#87: cap raised from 20 to 2000 so high-coverage orgs (Mission India,
    1,701 FPGs) validate. 2000 passes the schema layer; 2001 still bounces."""
    base = {"email": "cap@example.com", "display_name": "Cap"}
    ok = model.model_validate(
        {**base, "fpg_selections": [{"people_id3": f"R{i:04d}"} for i in range(2000)]}
    )
    assert len(ok.fpg_selections) == 2000
    with pytest.raises(ValidationError):
        model.model_validate(
            {**base, "fpg_selections": [{"people_id3": f"R{i:04d}"} for i in range(2001)]}
        )


async def _contact_submitted_rows(
    session: AsyncSession, contact_id: uuid.UUID
) -> list[Outbox]:
    return list(
        (
            await session.execute(
                select(Outbox).where(
                    Outbox.event_type == EVENT_CONTACT_SUBMITTED,
                    Outbox.payload_json["contact_id"].astext == str(contact_id),
                )
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_new_signup_emits_contact_submitted(session: AsyncSession) -> None:
    """A completed first-time form sign-up emits contact.submitted so the
    'Adopter sign-up welcome' drip (which triggers on that event) enrolls it."""
    email = f"helper-welcome-{uuid.uuid4().hex[:8]}@example.com"
    await _clean_email(session, email)
    payload = AdoptionIntake.model_validate(
        {
            "email": email,
            "display_name": "Welcome Me",
            "fpg_selections": [{"people_id3": "AAA01"}],
        }
    )
    outcome = await process_adoption_payload(
        session, payload=payload, settings=get_settings()
    )
    assert outcome.created is True
    rows = await _contact_submitted_rows(session, outcome.contact_id)
    assert len(rows) == 1
    assert rows[0].payload_json["party_kind"] == "adopter"
    await session.rollback()


@pytest.mark.asyncio
async def test_no_fpg_signup_still_emits_contact_submitted(
    session: AsyncSession,
) -> None:
    """A no-FPG signup lands as 'potential_adopter' but is still a complete
    sign-up, so it must also trigger the welcome."""
    email = f"helper-nofpg-{uuid.uuid4().hex[:8]}@example.com"
    await _clean_email(session, email)
    payload = AdoptionIntake.model_validate(
        {"email": email, "display_name": "No FPG", "fpg_selections": []}
    )
    outcome = await process_adoption_payload(
        session, payload=payload, settings=get_settings()
    )
    rows = await _contact_submitted_rows(session, outcome.contact_id)
    assert len(rows) == 1
    await session.rollback()


@pytest.mark.asyncio
async def test_returning_submitter_does_not_re_emit_contact_submitted(
    session: AsyncSession,
) -> None:
    """An already-existing contact (created=False) is in the funnel already and
    must not be re-welcomed on a repeat submission."""
    email = f"helper-return-{uuid.uuid4().hex[:8]}@example.com"
    await _clean_email(session, email)
    contact = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name="Returning",
        adopter_status="new",
        email_normalized=email.lower(),
    )
    session.add(contact)
    await session.commit()

    payload = AdoptionIntake.model_validate(
        {
            "email": email,
            "display_name": "Returning Again",
            "fpg_selections": [{"people_id3": "AAA01"}],
        }
    )
    outcome = await process_adoption_payload(
        session, payload=payload, settings=get_settings()
    )
    assert outcome.created is False
    rows = await _contact_submitted_rows(session, contact.id)
    assert rows == []
    await session.rollback()
    await _clean_email(session, email)


@pytest.mark.asyncio
async def test_blocked_submission_does_not_emit_contact_submitted(
    session: AsyncSession,
) -> None:
    """A do_not_engage contact short-circuits before the welcome emit."""
    email = f"helper-block-welcome-{uuid.uuid4().hex[:8]}@example.com"
    await _clean_email(session, email)
    contact = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name="Blocked",
        adopter_status="do_not_engage",
        email_normalized=email.lower(),
    )
    session.add(contact)
    await session.commit()

    payload = AdoptionIntake.model_validate(
        {
            "email": email,
            "display_name": "Blocked Welcome",
            "fpg_selections": [{"people_id3": "AAA01"}],
        }
    )
    outcome = await process_adoption_payload(
        session, payload=payload, settings=get_settings()
    )
    assert outcome.was_blocked is True
    rows = await _contact_submitted_rows(session, contact.id)
    assert rows == []
    await session.rollback()
    await _clean_email(session, email)
