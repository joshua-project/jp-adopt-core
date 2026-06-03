"""Worker task: ``send_contact_email_inline`` (F3).

The net-new behavior over the magic-link send is the post-send update of the
originating ``activity_log`` (email note) ``source_metadata.status``. These
tests exercise the three terminal states:

  * no ACS connection string (dev)        -> ``logged``
  * ACS configured but a send error       -> ``failed``
  * ACS configured + a successful send     -> ``sent`` (+ message_id)

ACS is stubbed via an injected fake ``azure.communication.email`` module so
the test does not require the optional SDK.
"""

from __future__ import annotations

import sys
import types
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jp_adopt_api.config import get_settings
from jp_adopt_api.models import ActivityLog, Contact
from jp_adopt_worker.tasks.send_contact_email import send_contact_email_inline


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _make_email_note(session: AsyncSession) -> tuple[Contact, ActivityLog]:
    contact = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name="Email Target",
        adopter_status="new",
        email_normalized=f"emailtgt-{uuid.uuid4().hex[:10]}@example.com",
        origin="worker_email_test",
    )
    session.add(contact)
    await session.flush()
    note = ActivityLog(
        id=uuid.uuid4(),
        contact_id=contact.id,
        author_id="dev-local",
        body="hi there",
        kind="email",
        source_system="local",
        source_metadata={"subject": "Hi", "to": [contact.email_normalized], "status": "queued"},
        occurred_at=datetime.now(UTC),
    )
    session.add(note)
    await session.commit()
    return contact, note


async def _cleanup(session: AsyncSession, contact: Contact) -> None:
    await session.execute(
        delete(ActivityLog).where(ActivityLog.contact_id == contact.id)
    )
    await session.execute(delete(Contact).where(Contact.id == contact.id))
    await session.commit()


def _inject_fake_acs(monkeypatch: pytest.MonkeyPatch, *, message_id: str) -> None:
    fake = types.ModuleType("azure.communication.email")

    class _Poller:
        def result(self) -> str:
            return message_id

    class _Client:
        @classmethod
        def from_connection_string(cls, _cs: str) -> "_Client":
            return cls()

        def begin_send(self, _message: dict) -> _Poller:
            return _Poller()

    fake.EmailClient = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "azure", types.ModuleType("azure"))
    monkeypatch.setitem(
        sys.modules, "azure.communication", types.ModuleType("azure.communication")
    )
    monkeypatch.setitem(sys.modules, "azure.communication.email", fake)


@pytest.mark.asyncio
async def test_dev_fallback_marks_note_logged(session: AsyncSession) -> None:
    contact, note = await _make_email_note(session)
    try:
        await send_contact_email_inline(
            note_id=note.id,
            recipients=[contact.email_normalized],
            subject="Hi",
            body="hi there",
            reply_to=None,
            acs_connection_string=None,  # dev: no ACS
            acs_sender_address="from@example.com",
            database_url=get_settings().database_url,
        )
        await session.refresh(note)
        assert note.source_metadata["status"] == "logged"
    finally:
        await _cleanup(session, contact)


@pytest.mark.asyncio
async def test_send_success_marks_note_sent_with_message_id(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _inject_fake_acs(monkeypatch, message_id="msg-abc-123")
    contact, note = await _make_email_note(session)
    try:
        await send_contact_email_inline(
            note_id=note.id,
            recipients=[contact.email_normalized],
            subject="Hi",
            body="hi there",
            reply_to="staff@example.com",
            acs_connection_string="endpoint=https://x.communication.azure.com/;accesskey=k",
            acs_sender_address="from@example.com",
            database_url=get_settings().database_url,
        )
        await session.refresh(note)
        assert note.source_metadata["status"] == "sent"
        assert note.source_metadata["message_id"] == "msg-abc-123"
    finally:
        await _cleanup(session, contact)


@pytest.mark.asyncio
async def test_send_error_marks_note_failed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fake client whose begin_send raises — exercises the failure path.
    fake = types.ModuleType("azure.communication.email")

    class _Client:
        @classmethod
        def from_connection_string(cls, _cs: str) -> "_Client":
            return cls()

        def begin_send(self, _message: dict) -> object:
            raise RuntimeError("acs unreachable")

    fake.EmailClient = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "azure", types.ModuleType("azure"))
    monkeypatch.setitem(
        sys.modules, "azure.communication", types.ModuleType("azure.communication")
    )
    monkeypatch.setitem(sys.modules, "azure.communication.email", fake)

    contact, note = await _make_email_note(session)
    try:
        await send_contact_email_inline(
            note_id=note.id,
            recipients=[contact.email_normalized],
            subject="Hi",
            body="hi there",
            reply_to=None,
            acs_connection_string="endpoint=https://x.communication.azure.com/;accesskey=k",
            acs_sender_address="from@example.com",
            database_url=get_settings().database_url,
        )
        await session.refresh(note)
        assert note.source_metadata["status"] == "failed"
    finally:
        await _cleanup(session, contact)
