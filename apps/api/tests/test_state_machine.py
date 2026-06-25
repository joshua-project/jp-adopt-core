"""Tests for U2 adoption state machine (domain/state_machine.py).

These tests exercise the ``transition_adopter`` / ``transition_facilitator``
entrypoints against a real Postgres instance (the same one used by
``test_foundation_migration.py``). Each test creates a fresh contact,
exercises one transition path, and rolls back via the session's own
``rollback`` so that we don't accumulate noise across runs.

Conftest defaults ``DATABASE_URL`` to the docker-compose Postgres on
``127.0.0.1:5434``. ``asyncio_mode=auto`` in ``pyproject.toml`` means
``async def`` test functions and async fixtures are picked up directly.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jp_adopt_api.config import get_settings
from jp_adopt_api.domain.state_machine import (
    ADOPTER_TRANSITIONS,
    ADOPTER_UNIVERSAL_TRANSITIONS,
    AdopterState,
    ConcurrentModificationError,
    FacilitatorState,
    IllegalTransitionError,
    InvalidReasonCodeError,
    ReasonCode,
    RoleNotPermittedError,
    TransitionSpec,
    available_transitions,
    transition_adopter,
    transition_facilitator,
)
from jp_adopt_api.models import Contact, Outbox, TransitionAudit

ACTOR_SUB = "user:test-actor"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Async SQLAlchemy session whose transaction rolls back at teardown.

    The state-machine functions ``flush()`` but never ``commit()``, so
    the outer rollback unwinds every Contact/Outbox/TransitionAudit row
    created during the test.
    """
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as s:
        try:
            yield s
        finally:
            await s.rollback()
    await engine.dispose()


async def _make_contact(
    session: AsyncSession,
    *,
    adopter_status: str | None = None,
    facilitator_status: str | None = None,
) -> Contact:
    contact = Contact(
        id=uuid.uuid4(),
        party_kind="adopter",
        display_name=f"Test Contact {uuid.uuid4()}",
        adopter_status=adopter_status,
        facilitator_status=facilitator_status,
    )
    session.add(contact)
    await session.flush()
    return contact


# ---------------------------------------------------------------------------
# Pure-Python table coverage tests (no DB).
# ---------------------------------------------------------------------------


def test_every_adopter_state_appears_in_transitions() -> None:
    """No orphan states: every AdopterState appears as either from_state
    or to_state in ADOPTER_TRANSITIONS, OR is the universal-DO_NOT_ENGAGE
    sink (which still references every other state implicitly)."""
    referenced: set[AdopterState] = set()
    for (frm, to) in ADOPTER_TRANSITIONS:
        referenced.add(frm)
        referenced.add(to)
    referenced.update(ADOPTER_UNIVERSAL_TRANSITIONS.keys())
    missing = set(AdopterState) - referenced
    assert not missing, f"orphan states with no transitions: {missing}"


def test_sent_back_spec_reason_optional_with_all_codes() -> None:
    """F2: the decline reason is optional, but when supplied it is still
    validated against the (all-codes) whitelist."""
    spec: TransitionSpec = ADOPTER_TRANSITIONS[
        (AdopterState.MATCHED, AdopterState.SENT_BACK)
    ]
    assert spec.requires_reason is False
    assert spec.reason_codes is not None
    assert ReasonCode.CAPACITY_FULL in spec.reason_codes
    assert ReasonCode.OTHER in spec.reason_codes


def test_available_transitions_for_adoption_manager_from_new() -> None:
    options = available_transitions(
        AdopterState.NEW, "adoption_manager", kind="adopter"
    )
    assert AdopterState.POTENTIAL_ADOPTER in options
    assert AdopterState.CONTACTED in options
    assert AdopterState.MATCHED in options
    assert AdopterState.DO_NOT_ENGAGE in options
    # adoption_manager cannot send_back from new (only from matched, and
    # only facilitator/admin) — verify it's absent.
    assert AdopterState.SENT_BACK not in options


def test_available_transitions_for_facilitator_role() -> None:
    options = available_transitions(
        AdopterState.MATCHED, "facilitator", kind="adopter"
    )
    assert AdopterState.SENT_BACK in options
    assert AdopterState.ACTIVE in options
    # Facilitator cannot mark do_not_engage (only adoption_manager/admin).
    assert AdopterState.DO_NOT_ENGAGE not in options


def test_adoption_manager_can_correct_engaged_backward() -> None:
    # Operator corrections: an engaged contact that was mis-classified can be
    # walked back to an earlier funnel state (e.g. still needs an FPG).
    options = available_transitions(
        AdopterState.ENGAGED, "adoption_manager", kind="adopter"
    )
    assert AdopterState.POTENTIAL_ADOPTER in options
    assert AdopterState.CONTACTED in options
    assert AdopterState.NEW in options
    # Forward + opt-out still available.
    assert AdopterState.MATCHED in options
    assert AdopterState.DO_NOT_ENGAGE in options


def test_facilitator_cannot_use_adopter_corrections() -> None:
    options = available_transitions(
        AdopterState.ENGAGED, "facilitator", kind="adopter"
    )
    # Corrections are adoption_manager/admin only.
    assert AdopterState.POTENTIAL_ADOPTER not in options
    assert AdopterState.CONTACTED not in options


# ---------------------------------------------------------------------------
# Async DB-backed transition tests.
# ---------------------------------------------------------------------------


async def test_happy_path_new_to_contacted(session: AsyncSession) -> None:
    contact = await _make_contact(session, adopter_status="new")
    original_version = contact.version

    updated = await transition_adopter(
        session,
        contact,
        to_state=AdopterState.CONTACTED,
        actor_b2c_sub=ACTOR_SUB,
        actor_role="adoption_manager",
    )

    assert updated.adopter_status == "contacted"
    assert updated.version == original_version + 1

    audit_rows = (
        (
            await session.execute(
                select(TransitionAudit).where(
                    TransitionAudit.contact_id == contact.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audit_rows) == 1
    assert audit_rows[0].from_state == "new"
    assert audit_rows[0].to_state == "contacted"
    assert audit_rows[0].actor_role == "adoption_manager"

    outbox_rows = (
        (
            await session.execute(
                select(Outbox).where(
                    Outbox.event_type == "jp.adopt.v1.contact.contacted"
                )
            )
        )
        .scalars()
        .all()
    )
    # Filter to this contact (other concurrent rows possible if tests
    # ever leak; in practice rollback isolates us).
    mine = [
        r for r in outbox_rows
        if r.payload_json.get("contact_id") == str(contact.id)
    ]
    assert len(mine) == 1
    assert mine[0].payload_json["from_state"] == "new"
    assert mine[0].payload_json["to_state"] == "contacted"


async def test_correction_engaged_to_potential_adopter(
    session: AsyncSession,
) -> None:
    # Amy's case: a contact imported as 'engaged' that actually still needs an
    # FPG can be corrected back to 'potential_adopter' by an adoption_manager.
    contact = await _make_contact(session, adopter_status="engaged")

    updated = await transition_adopter(
        session,
        contact,
        to_state=AdopterState.POTENTIAL_ADOPTER,
        actor_b2c_sub=ACTOR_SUB,
        actor_role="adoption_manager",
    )
    assert updated.adopter_status == "potential_adopter"

    # Emits the dedicated reclassified event (NOT a normal entry event), so no
    # drip campaign or intake side effect is triggered by a correction.
    reclassified = (
        (
            await session.execute(
                select(Outbox).where(
                    Outbox.event_type == "jp.adopt.v1.contact.reclassified"
                )
            )
        )
        .scalars()
        .all()
    )
    mine = [
        r for r in reclassified
        if r.payload_json.get("contact_id") == str(contact.id)
    ]
    assert len(mine) == 1
    assert mine[0].payload_json["from_state"] == "engaged"
    assert mine[0].payload_json["to_state"] == "potential_adopter"


async def test_correction_blocked_for_facilitator(session: AsyncSession) -> None:
    contact = await _make_contact(session, adopter_status="engaged")
    with pytest.raises(RoleNotPermittedError):
        await transition_adopter(
            session,
            contact,
            to_state=AdopterState.POTENTIAL_ADOPTER,
            actor_b2c_sub=ACTOR_SUB,
            actor_role="facilitator",
        )


async def test_happy_path_matched_to_sent_back_with_reason(
    session: AsyncSession,
) -> None:
    contact = await _make_contact(session, adopter_status="matched")

    await transition_adopter(
        session,
        contact,
        to_state=AdopterState.SENT_BACK,
        actor_b2c_sub=ACTOR_SUB,
        actor_role="facilitator",
        reason_code=ReasonCode.CAPACITY_FULL,
        reason_text="Already at 3/3 adoptions.",
    )

    assert contact.adopter_status == "sent_back"

    audit = (
        (
            await session.execute(
                select(TransitionAudit).where(
                    TransitionAudit.contact_id == contact.id
                )
            )
        )
        .scalars()
        .one()
    )
    assert audit.reason_code == "capacity_full"
    assert audit.reason_text == "Already at 3/3 adoptions."


async def test_illegal_transition_new_to_active(session: AsyncSession) -> None:
    contact = await _make_contact(session, adopter_status="new")

    with pytest.raises(IllegalTransitionError) as exc_info:
        await transition_adopter(
            session,
            contact,
            to_state=AdopterState.ACTIVE,
            actor_b2c_sub=ACTOR_SUB,
            actor_role="adoption_manager",
        )
    assert exc_info.value.from_state == AdopterState.NEW
    assert exc_info.value.to_state == AdopterState.ACTIVE


async def test_role_not_permitted(session: AsyncSession) -> None:
    contact = await _make_contact(session, adopter_status="matched")
    with pytest.raises(RoleNotPermittedError) as exc_info:
        await transition_adopter(
            session,
            contact,
            to_state=AdopterState.SENT_BACK,
            actor_b2c_sub=ACTOR_SUB,
            actor_role="adoption_partner",  # not a seeded role; not in allowed set
            reason_code=ReasonCode.CAPACITY_FULL,
        )
    assert exc_info.value.actor_role == "adoption_partner"


async def test_sent_back_without_reason_succeeds(session: AsyncSession) -> None:
    """F2: matched → sent_back with no reason_code is now legal."""
    contact = await _make_contact(session, adopter_status="matched")
    await transition_adopter(
        session,
        contact,
        to_state=AdopterState.SENT_BACK,
        actor_b2c_sub=ACTOR_SUB,
        actor_role="facilitator",
        reason_code=None,
    )
    assert contact.adopter_status == "sent_back"


async def test_sent_back_with_reason_other_succeeds(
    session: AsyncSession,
) -> None:
    contact = await _make_contact(session, adopter_status="matched")
    await transition_adopter(
        session,
        contact,
        to_state=AdopterState.SENT_BACK,
        actor_b2c_sub=ACTOR_SUB,
        actor_role="facilitator",
        reason_code=ReasonCode.OTHER,
        reason_text="other reason",
    )
    assert contact.adopter_status == "sent_back"


async def test_invalid_reason_code_rejected() -> None:
    """When a spec restricts ``reason_codes`` and the value falls outside
    that whitelist, raise ``InvalidReasonCodeError``. We construct a
    one-off spec inline to verify the validator behavior independent of
    DB state, since every real send-back spec accepts all codes."""
    from jp_adopt_api.domain.state_machine import _validate_reason

    narrow = TransitionSpec(
        allowed_roles=frozenset({"x"}),
        requires_reason=True,
        event_type="x",
        reason_codes=frozenset({ReasonCode.CAPACITY_FULL}),
    )
    with pytest.raises(InvalidReasonCodeError):
        _validate_reason(narrow, ReasonCode.OTHER)


async def test_do_not_engage_from_new(session: AsyncSession) -> None:
    contact = await _make_contact(session, adopter_status="new")
    await transition_adopter(
        session,
        contact,
        to_state=AdopterState.DO_NOT_ENGAGE,
        actor_b2c_sub=ACTOR_SUB,
        actor_role="adoption_manager",
    )
    assert contact.adopter_status == "do_not_engage"

    outbox_rows = (
        (
            await session.execute(
                select(Outbox).where(
                    Outbox.event_type == "jp.adopt.v1.contact.do_not_engage"
                )
            )
        )
        .scalars()
        .all()
    )
    mine = [
        r for r in outbox_rows
        if r.payload_json.get("contact_id") == str(contact.id)
    ]
    assert len(mine) == 1
    assert mine[0].payload_json["from_state"] == "new"


async def test_do_not_engage_from_matched(session: AsyncSession) -> None:
    contact = await _make_contact(session, adopter_status="matched")
    await transition_adopter(
        session,
        contact,
        to_state=AdopterState.DO_NOT_ENGAGE,
        actor_b2c_sub=ACTOR_SUB,
        actor_role="staff_admin",
    )
    assert contact.adopter_status == "do_not_engage"


async def test_concurrent_modification_raises(session: AsyncSession) -> None:
    contact = await _make_contact(session, adopter_status="new")
    # Simulate a stale in-memory version — DB has version=1, we pretend
    # we last saw version=0 ("someone updated under us").
    contact.version = contact.version - 1

    with pytest.raises(ConcurrentModificationError) as exc_info:
        await transition_adopter(
            session,
            contact,
            to_state=AdopterState.CONTACTED,
            actor_b2c_sub=ACTOR_SUB,
            actor_role="adoption_manager",
        )
    assert exc_info.value.contact_id == contact.id
    assert exc_info.value.expected_version + 1 == exc_info.value.actual_version


async def test_outbox_payload_carries_reason(session: AsyncSession) -> None:
    contact = await _make_contact(session, adopter_status="matched")
    await transition_adopter(
        session,
        contact,
        to_state=AdopterState.SENT_BACK,
        actor_b2c_sub=ACTOR_SUB,
        actor_role="facilitator",
        reason_code=ReasonCode.CAPACITY_FULL,
        reason_text="full",
    )

    outbox = (
        (
            await session.execute(
                select(Outbox).where(
                    Outbox.event_type == "jp.adopt.v1.match.sent_back"
                )
            )
        )
        .scalars()
        .all()
    )
    mine = [r for r in outbox if r.payload_json.get("contact_id") == str(contact.id)]
    assert len(mine) == 1
    assert mine[0].payload_json["reason_code"] == "capacity_full"
    assert mine[0].payload_json["actor"] == {
        "sub": ACTOR_SUB,
        "role": "facilitator",
    }


async def test_facilitator_transition_new_to_ready(session: AsyncSession) -> None:
    contact = await _make_contact(
        session, facilitator_status="new"
    )
    await transition_facilitator(
        session,
        contact,
        to_state=FacilitatorState.READY,
        actor_b2c_sub=ACTOR_SUB,
        actor_role="adoption_manager",
    )
    assert contact.facilitator_status == "ready"


# ---------------------------------------------------------------------------
# F4 regression: transition Outbox writes route through emit_outbox so the
# outbox_suppressed() bulk-import context can swallow them.
# ---------------------------------------------------------------------------


async def test_transition_under_outbox_suppression_emits_zero_outbox_rows(
    session: AsyncSession,
) -> None:
    """One transition inside ``outbox_suppressed`` produces 0 per-row Outbox
    rows and increments the suppression counter by 1.

    The summary ``jp.adopt.v1.bulk_imported`` event is still emitted by the
    context manager on exit; that is the single row we expect to see for
    the suppressed event_type, NOT the per-transition event itself.
    """
    from jp_adopt_api.outbox_suppression import outbox_suppressed

    contact = await _make_contact(session, adopter_status="new")
    event_type = "jp.adopt.v1.contact.contacted"

    async with outbox_suppressed("test_bulk_label", session) as ctx:
        await transition_adopter(
            session,
            contact,
            to_state=AdopterState.CONTACTED,
            actor_b2c_sub=ACTOR_SUB,
            actor_role="adoption_manager",
        )
        # Inside the suppression context: the per-event row was buffered,
        # not written. The suppression counter records what was skipped.
        assert ctx.event_counts[event_type] == 1
        assert ctx.total_suppressed == 1

    # Now after the context exits, verify that no Outbox row for this
    # contact's contacted event was actually written, and that the audit
    # row honestly stored ``outbox_event_ids = NULL`` (no fake UUID).
    per_event_rows = (
        (
            await session.execute(
                select(Outbox).where(Outbox.event_type == event_type)
            )
        )
        .scalars()
        .all()
    )
    mine = [
        r for r in per_event_rows
        if r.payload_json.get("contact_id") == str(contact.id)
    ]
    assert mine == [], "per-row Outbox event must not be written under suppression"

    audit = (
        (
            await session.execute(
                select(TransitionAudit).where(
                    TransitionAudit.contact_id == contact.id
                )
            )
        )
        .scalars()
        .one()
    )
    assert audit.outbox_event_ids is None
