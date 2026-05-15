"""Adoption / facilitator processing state machine (U2).

Hand-rolled enum + transition table. Single ``transition_adopter`` /
``transition_facilitator`` entrypoint that:

* Validates ``(from_state, to_state)`` against the transition table.
* Validates the actor's role against the spec's ``allowed_roles``.
* Enforces ``requires_reason`` and ``reason_codes`` whitelists.
* Implements optimistic concurrency on ``contacts.version`` via
  ``SELECT ... FOR UPDATE``.
* Atomically updates the Contact row, writes a TransitionAudit row, and
  emits an Outbox event. The function ``flush()``-es but does NOT
  ``commit()`` — the caller controls the transaction boundary, mirroring
  the existing pattern used in routers (see ``routers/contacts.py``).

The transition table also doubles as the source of truth for "what
actions can this user take from this state?" via
``available_transitions()``.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jp_adopt_api.models import Contact, Outbox, TransitionAudit

# ---------------------------------------------------------------------------
# Enums (lowercase values, exactly matching the CHECK constraint strings on
# ``contacts.adopter_status`` and ``contacts.facilitator_status``).
# ---------------------------------------------------------------------------


class AdopterState(enum.StrEnum):
    DRAFT = "draft"
    NEW = "new"
    POTENTIAL_ADOPTER = "potential_adopter"
    CONTACTED = "contacted"
    ENGAGED = "engaged"
    MATCHED = "matched"
    SENT_BACK = "sent_back"
    ACTIVE = "active"
    INACTIVE = "inactive"
    DO_NOT_ENGAGE = "do_not_engage"


class FacilitatorState(enum.StrEnum):
    DRAFT = "draft"
    NEW = "new"
    NOT_READY = "not_ready"
    READY = "ready"
    DO_NOT_ENGAGE = "do_not_engage"


class ReasonCode(enum.StrEnum):
    CAPACITY_FULL = "capacity_full"
    GEOGRAPHY_MISMATCH = "geography_mismatch"
    LANGUAGE = "language"
    THEOLOGICAL_CONCERN = "theological_concern"
    NOT_READY = "not_ready"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Role identifiers (string literals, matching the seeded ``roles.name`` set).
# ``adoption_partner`` is intentionally NOT in the seeded role set; tests
# pass it as a string to verify role-check behavior without a DB lookup.
# ---------------------------------------------------------------------------

ROLE_STAFF_ADMIN = "staff_admin"
ROLE_ADOPTION_MANAGER = "adoption_manager"
ROLE_TRIAGE_FACILITATOR = "triage_facilitator"
ROLE_FACILITATOR = "facilitator"

ANY_ROLE = frozenset(
    {
        ROLE_STAFF_ADMIN,
        ROLE_ADOPTION_MANAGER,
        ROLE_TRIAGE_FACILITATOR,
        ROLE_FACILITATOR,
    }
)


# ---------------------------------------------------------------------------
# TransitionSpec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransitionSpec:
    allowed_roles: frozenset[str]
    requires_reason: bool
    event_type: str
    reason_codes: frozenset[ReasonCode] | None = None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class IllegalTransitionError(ValueError):
    """Raised when ``(from_state, to_state)`` is not in the transition table."""

    def __init__(self, from_state: enum.StrEnum, to_state: enum.StrEnum) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"Illegal transition: {from_state.value} -> {to_state.value}"
        )


class RoleNotPermittedError(PermissionError):
    """Raised when the actor's role is not in ``spec.allowed_roles``."""

    def __init__(self, actor_role: str, required_roles: frozenset[str]) -> None:
        self.actor_role = actor_role
        self.required_roles = required_roles
        super().__init__(
            f"Role {actor_role!r} is not permitted; "
            f"required one of {sorted(required_roles)}"
        )


class ReasonRequiredError(ValueError):
    """Raised when ``spec.requires_reason`` is True but ``reason_code`` is None."""


class InvalidReasonCodeError(ValueError):
    """Raised when a provided ``reason_code`` is not in ``spec.reason_codes``."""

    def __init__(
        self,
        reason_code: ReasonCode,
        allowed: frozenset[ReasonCode],
    ) -> None:
        self.reason_code = reason_code
        self.allowed = allowed
        super().__init__(
            f"Reason code {reason_code.value!r} is not allowed; "
            f"required one of {sorted(c.value for c in allowed)}"
        )


class ConcurrentModificationError(RuntimeError):
    """Raised when ``contacts.version`` in DB no longer matches the in-memory
    version (someone else updated the row first)."""

    def __init__(
        self,
        contact_id: uuid.UUID,
        expected_version: int,
        actual_version: int,
    ) -> None:
        self.contact_id = contact_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"Concurrent modification on contact {contact_id}: "
            f"expected version {expected_version}, got {actual_version}"
        )


# ---------------------------------------------------------------------------
# Transition tables
# ---------------------------------------------------------------------------

_ADOPTION_MANAGER_OR_ADMIN = frozenset({ROLE_ADOPTION_MANAGER, ROLE_STAFF_ADMIN})
_ADOPTION_MANAGER_TRIAGE_ADMIN = frozenset(
    {ROLE_ADOPTION_MANAGER, ROLE_TRIAGE_FACILITATOR, ROLE_STAFF_ADMIN}
)
_FACILITATOR_OR_ADMIN = frozenset({ROLE_FACILITATOR, ROLE_STAFF_ADMIN})

_ALL_REASON_CODES = frozenset(ReasonCode)


EVENT_CONTACT_SUBMITTED = "jp.adopt.v1.contact.submitted"
EVENT_CONTACT_TRIAGED_NO_FPG = "jp.adopt.v1.contact.triaged_no_fpg"
EVENT_CONTACT_CONTACTED = "jp.adopt.v1.contact.contacted"
EVENT_CONTACT_ENGAGED = "jp.adopt.v1.contact.engaged"
EVENT_MATCH_ASSIGNED = "jp.adopt.v1.match.assigned"
EVENT_MATCH_SENT_BACK = "jp.adopt.v1.match.sent_back"
EVENT_MATCH_REASSIGNED = "jp.adopt.v1.match.reassigned"
EVENT_MATCH_ACCEPTED = "jp.adopt.v1.match.accepted"
EVENT_CONTACT_DEACTIVATED = "jp.adopt.v1.contact.deactivated"
EVENT_CONTACT_DO_NOT_ENGAGE = "jp.adopt.v1.contact.do_not_engage"

EVENT_FACILITATOR_SUBMITTED = "jp.adopt.v1.facilitator.submitted"
EVENT_FACILITATOR_MARKED_NOT_READY = "jp.adopt.v1.facilitator.marked_not_ready"
EVENT_FACILITATOR_MARKED_READY = "jp.adopt.v1.facilitator.marked_ready"
EVENT_FACILITATOR_DO_NOT_ENGAGE = "jp.adopt.v1.facilitator.do_not_engage"


ADOPTER_TRANSITIONS: dict[tuple[AdopterState, AdopterState], TransitionSpec] = {
    (AdopterState.DRAFT, AdopterState.NEW): TransitionSpec(
        allowed_roles=ANY_ROLE,
        requires_reason=False,
        event_type=EVENT_CONTACT_SUBMITTED,
    ),
    (AdopterState.NEW, AdopterState.POTENTIAL_ADOPTER): TransitionSpec(
        allowed_roles=_ADOPTION_MANAGER_TRIAGE_ADMIN,
        requires_reason=False,
        event_type=EVENT_CONTACT_TRIAGED_NO_FPG,
    ),
    (AdopterState.NEW, AdopterState.CONTACTED): TransitionSpec(
        allowed_roles=_ADOPTION_MANAGER_OR_ADMIN,
        requires_reason=False,
        event_type=EVENT_CONTACT_CONTACTED,
    ),
    (AdopterState.POTENTIAL_ADOPTER, AdopterState.CONTACTED): TransitionSpec(
        allowed_roles=_ADOPTION_MANAGER_TRIAGE_ADMIN,
        requires_reason=False,
        event_type=EVENT_CONTACT_CONTACTED,
    ),
    (AdopterState.CONTACTED, AdopterState.ENGAGED): TransitionSpec(
        allowed_roles=_ADOPTION_MANAGER_OR_ADMIN,
        requires_reason=False,
        event_type=EVENT_CONTACT_ENGAGED,
    ),
    (AdopterState.ENGAGED, AdopterState.MATCHED): TransitionSpec(
        allowed_roles=_ADOPTION_MANAGER_OR_ADMIN,
        requires_reason=False,
        event_type=EVENT_MATCH_ASSIGNED,
    ),
    # Amy-shortcut: pre-active states can fast-forward to matched.
    (AdopterState.NEW, AdopterState.MATCHED): TransitionSpec(
        allowed_roles=_ADOPTION_MANAGER_OR_ADMIN,
        requires_reason=False,
        event_type=EVENT_MATCH_ASSIGNED,
    ),
    (AdopterState.POTENTIAL_ADOPTER, AdopterState.MATCHED): TransitionSpec(
        allowed_roles=_ADOPTION_MANAGER_OR_ADMIN,
        requires_reason=False,
        event_type=EVENT_MATCH_ASSIGNED,
    ),
    (AdopterState.CONTACTED, AdopterState.MATCHED): TransitionSpec(
        allowed_roles=_ADOPTION_MANAGER_OR_ADMIN,
        requires_reason=False,
        event_type=EVENT_MATCH_ASSIGNED,
    ),
    (AdopterState.MATCHED, AdopterState.SENT_BACK): TransitionSpec(
        allowed_roles=_FACILITATOR_OR_ADMIN,
        requires_reason=True,
        event_type=EVENT_MATCH_SENT_BACK,
        reason_codes=_ALL_REASON_CODES,
    ),
    (AdopterState.SENT_BACK, AdopterState.MATCHED): TransitionSpec(
        allowed_roles=_ADOPTION_MANAGER_OR_ADMIN,
        requires_reason=False,
        event_type=EVENT_MATCH_REASSIGNED,
    ),
    (AdopterState.MATCHED, AdopterState.ACTIVE): TransitionSpec(
        allowed_roles=_FACILITATOR_OR_ADMIN,
        requires_reason=False,
        event_type=EVENT_MATCH_ACCEPTED,
    ),
    (AdopterState.ACTIVE, AdopterState.INACTIVE): TransitionSpec(
        allowed_roles=_ADOPTION_MANAGER_OR_ADMIN,
        requires_reason=False,
        event_type=EVENT_CONTACT_DEACTIVATED,
    ),
}


# Universal "any state -> DO_NOT_ENGAGE" path. The transition() function
# checks this lookup BEFORE consulting ADOPTER_TRANSITIONS for a
# (from_state, DO_NOT_ENGAGE) pair, so every source state is covered
# without enumerating each cell explicitly.
ADOPTER_UNIVERSAL_TRANSITIONS: dict[AdopterState, TransitionSpec] = {
    AdopterState.DO_NOT_ENGAGE: TransitionSpec(
        allowed_roles=_ADOPTION_MANAGER_OR_ADMIN,
        requires_reason=False,
        event_type=EVENT_CONTACT_DO_NOT_ENGAGE,
    ),
}


FACILITATOR_TRANSITIONS: dict[
    tuple[FacilitatorState, FacilitatorState], TransitionSpec
] = {
    (FacilitatorState.DRAFT, FacilitatorState.NEW): TransitionSpec(
        allowed_roles=ANY_ROLE,
        requires_reason=False,
        event_type=EVENT_FACILITATOR_SUBMITTED,
    ),
    (FacilitatorState.NEW, FacilitatorState.NOT_READY): TransitionSpec(
        allowed_roles=_ADOPTION_MANAGER_OR_ADMIN,
        requires_reason=False,
        event_type=EVENT_FACILITATOR_MARKED_NOT_READY,
    ),
    (FacilitatorState.NEW, FacilitatorState.READY): TransitionSpec(
        allowed_roles=_ADOPTION_MANAGER_OR_ADMIN,
        requires_reason=False,
        event_type=EVENT_FACILITATOR_MARKED_READY,
    ),
    (FacilitatorState.NOT_READY, FacilitatorState.READY): TransitionSpec(
        allowed_roles=_ADOPTION_MANAGER_OR_ADMIN,
        requires_reason=False,
        event_type=EVENT_FACILITATOR_MARKED_READY,
    ),
}


FACILITATOR_UNIVERSAL_TRANSITIONS: dict[FacilitatorState, TransitionSpec] = {
    FacilitatorState.DO_NOT_ENGAGE: TransitionSpec(
        allowed_roles=_ADOPTION_MANAGER_OR_ADMIN,
        requires_reason=False,
        event_type=EVENT_FACILITATOR_DO_NOT_ENGAGE,
    ),
}


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def _lookup_adopter_spec(
    from_state: AdopterState, to_state: AdopterState
) -> TransitionSpec:
    if to_state in ADOPTER_UNIVERSAL_TRANSITIONS and from_state != to_state:
        return ADOPTER_UNIVERSAL_TRANSITIONS[to_state]
    spec = ADOPTER_TRANSITIONS.get((from_state, to_state))
    if spec is None:
        raise IllegalTransitionError(from_state, to_state)
    return spec


def _lookup_facilitator_spec(
    from_state: FacilitatorState, to_state: FacilitatorState
) -> TransitionSpec:
    if to_state in FACILITATOR_UNIVERSAL_TRANSITIONS and from_state != to_state:
        return FACILITATOR_UNIVERSAL_TRANSITIONS[to_state]
    spec = FACILITATOR_TRANSITIONS.get((from_state, to_state))
    if spec is None:
        raise IllegalTransitionError(from_state, to_state)
    return spec


def available_transitions(
    current_state: AdopterState | FacilitatorState,
    actor_role: str,
    *,
    kind: Literal["adopter", "facilitator"],
) -> list[AdopterState] | list[FacilitatorState]:
    """Return the list of to-states the ``actor_role`` is permitted to move
    to from ``current_state``. Includes the universal DO_NOT_ENGAGE path
    when permitted. Order is deterministic (enum declaration order).
    """
    if kind == "adopter":
        if not isinstance(current_state, AdopterState):
            raise TypeError("kind='adopter' requires AdopterState input")
        adopter_out: list[AdopterState] = []
        for (frm, to), spec in ADOPTER_TRANSITIONS.items():
            if frm == current_state and actor_role in spec.allowed_roles:
                adopter_out.append(to)
        for to, spec in ADOPTER_UNIVERSAL_TRANSITIONS.items():
            if current_state == to:
                continue
            if actor_role in spec.allowed_roles and to not in adopter_out:
                adopter_out.append(to)
        return sorted(adopter_out, key=lambda s: list(AdopterState).index(s))

    if kind == "facilitator":
        if not isinstance(current_state, FacilitatorState):
            raise TypeError("kind='facilitator' requires FacilitatorState input")
        facilitator_out: list[FacilitatorState] = []
        for (frm, to), spec in FACILITATOR_TRANSITIONS.items():
            if frm == current_state and actor_role in spec.allowed_roles:
                facilitator_out.append(to)
        for to, spec in FACILITATOR_UNIVERSAL_TRANSITIONS.items():
            if current_state == to:
                continue
            if actor_role in spec.allowed_roles and to not in facilitator_out:
                facilitator_out.append(to)
        return sorted(
            facilitator_out, key=lambda s: list(FacilitatorState).index(s)
        )

    raise ValueError(f"Unknown kind: {kind!r}")


# ---------------------------------------------------------------------------
# Core transition entrypoints
# ---------------------------------------------------------------------------


def _validate_reason(
    spec: TransitionSpec, reason_code: ReasonCode | None
) -> None:
    if spec.requires_reason and reason_code is None:
        raise ReasonRequiredError(
            "A reason_code is required for this transition"
        )
    if (
        reason_code is not None
        and spec.reason_codes is not None
        and reason_code not in spec.reason_codes
    ):
        raise InvalidReasonCodeError(reason_code, spec.reason_codes)


async def _lock_and_check_version(
    session: AsyncSession, contact: Contact
) -> None:
    """``SELECT ... FOR UPDATE`` the contact row by primary key and verify
    the DB-side ``version`` matches what the caller thinks it is.

    We query the ``version`` column directly (rather than re-hydrating
    the ORM object) so that SQLAlchemy's identity map does not silently
    return the same in-memory ``contact`` instance — which would defeat
    the optimistic-lock check.
    """
    result = await session.execute(
        select(Contact.version)
        .where(Contact.id == contact.id)
        .with_for_update()
    )
    db_version = result.scalar_one()
    if db_version != contact.version:
        raise ConcurrentModificationError(
            contact_id=contact.id,
            expected_version=contact.version,
            actual_version=db_version,
        )


def _build_payload(
    *,
    event_type: str,
    contact_id: uuid.UUID,
    from_state: enum.StrEnum,
    to_state: enum.StrEnum,
    actor_b2c_sub: str,
    actor_role: str,
    reason_code: ReasonCode | None,
    reason_text: str | None,
    timestamp: datetime,
) -> dict[str, object]:
    return {
        "event": event_type,
        "schema_version": "1",
        "timestamp": timestamp.isoformat(),
        "contact_id": str(contact_id),
        "from_state": from_state.value,
        "to_state": to_state.value,
        "actor": {"sub": actor_b2c_sub, "role": actor_role},
        "reason_code": reason_code.value if reason_code is not None else None,
        "reason_text": reason_text,
    }


async def transition_adopter(
    session: AsyncSession,
    contact: Contact,
    *,
    to_state: AdopterState,
    actor_b2c_sub: str,
    actor_role: str,
    reason_code: ReasonCode | None = None,
    reason_text: str | None = None,
) -> Contact:
    """Move ``contact`` along the adopter side of the state machine.

    Caller controls the transaction boundary — this function flushes but
    does NOT commit. Raises one of the documented exceptions on any
    validation failure.
    """
    if contact.adopter_status is None:
        from_state = AdopterState.DRAFT
    else:
        try:
            from_state = AdopterState(contact.adopter_status)
        except ValueError as e:
            raise IllegalTransitionError(
                AdopterState.DRAFT, to_state
            ) from e

    spec = _lookup_adopter_spec(from_state, to_state)

    if actor_role not in spec.allowed_roles:
        raise RoleNotPermittedError(actor_role, spec.allowed_roles)

    _validate_reason(spec, reason_code)

    await _lock_and_check_version(session, contact)

    now = datetime.now(UTC)
    payload = _build_payload(
        event_type=spec.event_type,
        contact_id=contact.id,
        from_state=from_state,
        to_state=to_state,
        actor_b2c_sub=actor_b2c_sub,
        actor_role=actor_role,
        reason_code=reason_code,
        reason_text=reason_text,
        timestamp=now,
    )

    outbox = Outbox(
        id=uuid.uuid4(),
        event_type=spec.event_type,
        payload_json=payload,
    )
    session.add(outbox)

    audit = TransitionAudit(
        id=uuid.uuid4(),
        contact_id=contact.id,
        from_state=from_state.value,
        to_state=to_state.value,
        actor_id=actor_b2c_sub,
        actor_role=actor_role,
        reason_code=reason_code.value if reason_code is not None else None,
        reason_text=reason_text,
        outbox_event_ids=[outbox.id],
    )
    session.add(audit)

    contact.adopter_status = to_state.value
    contact.version = contact.version + 1
    contact.updated_at = now

    await session.flush()
    return contact


async def transition_facilitator(
    session: AsyncSession,
    contact: Contact,
    *,
    to_state: FacilitatorState,
    actor_b2c_sub: str,
    actor_role: str,
    reason_code: ReasonCode | None = None,
    reason_text: str | None = None,
) -> Contact:
    """Move ``contact`` along the facilitator side of the state machine.

    Caller controls the transaction boundary — this function flushes but
    does NOT commit.
    """
    if contact.facilitator_status is None:
        from_state = FacilitatorState.DRAFT
    else:
        try:
            from_state = FacilitatorState(contact.facilitator_status)
        except ValueError as e:
            raise IllegalTransitionError(
                FacilitatorState.DRAFT, to_state
            ) from e

    spec = _lookup_facilitator_spec(from_state, to_state)

    if actor_role not in spec.allowed_roles:
        raise RoleNotPermittedError(actor_role, spec.allowed_roles)

    _validate_reason(spec, reason_code)

    await _lock_and_check_version(session, contact)

    now = datetime.now(UTC)
    payload = _build_payload(
        event_type=spec.event_type,
        contact_id=contact.id,
        from_state=from_state,
        to_state=to_state,
        actor_b2c_sub=actor_b2c_sub,
        actor_role=actor_role,
        reason_code=reason_code,
        reason_text=reason_text,
        timestamp=now,
    )

    outbox = Outbox(
        id=uuid.uuid4(),
        event_type=spec.event_type,
        payload_json=payload,
    )
    session.add(outbox)

    audit = TransitionAudit(
        id=uuid.uuid4(),
        contact_id=contact.id,
        from_state=from_state.value,
        to_state=to_state.value,
        actor_id=actor_b2c_sub,
        actor_role=actor_role,
        reason_code=reason_code.value if reason_code is not None else None,
        reason_text=reason_text,
        outbox_event_ids=[outbox.id],
    )
    session.add(audit)

    contact.facilitator_status = to_state.value
    contact.version = contact.version + 1
    contact.updated_at = now

    await session.flush()
    return contact


__all__ = [
    "ADOPTER_TRANSITIONS",
    "ADOPTER_UNIVERSAL_TRANSITIONS",
    "FACILITATOR_TRANSITIONS",
    "FACILITATOR_UNIVERSAL_TRANSITIONS",
    "AdopterState",
    "ConcurrentModificationError",
    "FacilitatorState",
    "IllegalTransitionError",
    "InvalidReasonCodeError",
    "ReasonCode",
    "ReasonRequiredError",
    "RoleNotPermittedError",
    "TransitionSpec",
    "available_transitions",
    "transition_adopter",
    "transition_facilitator",
]
