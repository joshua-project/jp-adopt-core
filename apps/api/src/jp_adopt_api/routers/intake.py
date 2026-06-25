"""Form A / Form B intake endpoints (U4).

Receives adoption + facilitation submissions from `jp-adopt-forms`. Mirrors
that repo's envelope, error codes, and 64KB body limit verbatim so jp-adopt-
forms can dual-write to its local DB and to this API with no code-path
divergence.

Endpoints
---------
* `POST /v1/intake/adoption`     — Form B (`/adopt`) submissions
* `POST /v1/intake/facilitation` — Form A (`/facilitate-adoption`) submissions

Both require the `Idempotency-Key` header (24h dedup window) and `Authorization:
Bearer <api_key>` matching one of the configured `INTAKE_API_KEYS`.

Status codes (matching the upstream contract):
* `201 Created`              — first-ever processing of this idempotency key
* `200 OK`                   — replay: cached response from a prior call with
                               the same (api_key, idempotency_key)
* `400 validation_failed`    — body shape rejected by Pydantic / our validators
* `400 idempotency_required` — missing `Idempotency-Key` header
* `401 unauthorized`         — missing / unknown bearer
* `413 payload_too_large`    — body exceeded `INTAKE_MAX_BODY_BYTES`
* `422 idempotency_key_conflict` — same key, different body hash
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Header, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from jp_adopt_api.config import Settings
from jp_adopt_api.deps import DbSession, SettingsDep
from jp_adopt_api.domain.state_machine import EVENT_CONTACT_SUBMITTED
from jp_adopt_api.email_utils import normalize_email
from jp_adopt_api.models import (
    AdopterInterest,
    ApiIdempotencyKey,
    Consent,
    Contact,
    ContactProfile,
    Fpg,
    SubmissionBlocked,
)
from jp_adopt_api.outbox_suppression import emit_outbox
from jp_adopt_api.schemas import (
    AdoptionIntake,
    ConsentIn,
    ContactProfileIntake,
    FacilitationIntake,
    FpgInterestIn,
    IntakeError,
    IntakeErrorBody,
    IntakeSuccess,
    IntakeSuccessData,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/intake", tags=["intake"])

# 1MB body limit. Raised from 64KB per #87: legit high-coverage facilitation
# submissions (Mission India's 1,701 FPGs) serialize past 64KB. 2000-entry
# fpg_selections cap (see schemas.py) bounds the worst case well under 1MB.
INTAKE_MAX_BODY_BYTES = 1024 * 1024

EVENT_SUBMISSION_RECEIVED = "jp.adopt.v1.submission.received"

SOURCE_SYSTEM_FORMS = "jp-adopt-forms"


@dataclass
class IntakeOutcome:
    """Plain result from :func:`process_adoption_payload` /
    :func:`process_facilitation_payload`. HTTP wrappers JSON-encode this;
    batch importers (forms-etl) consume it directly."""

    contact_id: uuid.UUID
    created: bool
    interest_ids: list[uuid.UUID]
    was_blocked: bool
    submission_id: uuid.UUID


class IntakeValidationError(Exception):
    """Raised when intake payload fails validation (mirrors HTTP 400 paths)."""

    def __init__(
        self,
        *,
        code: str = "validation_failed",
        message: str | None = None,
        fields: dict[str, list[str]] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.fields = fields
        super().__init__(message or code)


# Loose email shape gate before we hand off to email-validator (which is
# strict but slow on garbage input). RFC 5321 caps local-part at 64 + 1 +
# 255; we cap the whole address at 320.
_EMAIL_SANITY = re.compile(r"^[^@\s]{1,64}@[^@\s]{3,255}$")


# ──────────────────────────────────────────────────────────────────────────
# Response helpers
# ──────────────────────────────────────────────────────────────────────────


def _request_id() -> str:
    """Server-side request UUID; appears in both success + error envelopes."""
    return str(uuid.uuid4())


def _error_response(
    status_code: int,
    *,
    code: str,
    request_id: str,
    message: str | None = None,
    fields: dict[str, list[str]] | None = None,
    **extra: object,
) -> JSONResponse:
    body = IntakeError(
        error=IntakeErrorBody(
            code=code,
            message=message,
            fields=fields,
            request_id=request_id,
        )
    ).model_dump(mode="json", by_alias=True, exclude_none=True)
    if extra:
        body["error"].update(extra)
    return JSONResponse(status_code=status_code, content=body)


def _success_response(
    status_code: int,
    *,
    submission_id: uuid.UUID,
    request_id: str,
    contact_id: uuid.UUID,
    interest_ids: list[uuid.UUID],
) -> JSONResponse:
    body = IntakeSuccess(
        data=IntakeSuccessData(
            submission_id=submission_id,
            request_id=request_id,
            contact_id=contact_id,
            interest_ids=interest_ids,
        )
    ).model_dump(mode="json", by_alias=True)
    return JSONResponse(status_code=status_code, content=body)


# ──────────────────────────────────────────────────────────────────────────
# Auth + idempotency primitives
# ──────────────────────────────────────────────────────────────────────────


async def _authenticate(
    authorization: str | None,
    settings: Settings,
    session: AsyncSession,
    request: Request,
) -> str | None:
    """Return the api_key_id label on success, else None.

    #59: look-up order is DB-first, env-var-fallback. This lets the
    self-managed key store coexist with the legacy
    ``INTAKE_API_KEYS`` allowlist during the rotation window. The
    fallback path will be removed once every consumer is on a
    DB-issued key.

    DB-hit side effect: record ``last_used_at`` + IP + UA on the
    matched row. Errors during this update are swallowed — auth
    should not fail because the audit trail couldn't be written.

    The returned label is the SHA-256/16-hex of the matched key
    (NOT the raw bearer), so the ``api_idempotency_keys`` table
    never persists a usable credential.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return None

    # 1) DB lookup: hash the token and check for an unrevoked match.
    from jp_adopt_api.models import IntakeApiKey  # local import — avoid cycle

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    row = (
        await session.execute(
            select(IntakeApiKey).where(
                IntakeApiKey.key_hash == token_hash,
                IntakeApiKey.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        try:
            row.last_used_at = datetime.now(UTC)
            row.last_used_ip = (
                request.client.host if request.client is not None else None
            )
            row.last_used_user_agent = request.headers.get("user-agent")
            await session.flush()
        except Exception as e:  # noqa: BLE001 — audit miss must not break auth
            logger.warning("intake_last_used_update_failed err=%s", e)
        return token_hash[:16]

    # 2) Env-var fallback: legacy allowlist (deprecated; remove once
    #    every consumer is on a DB-issued key).
    accepted = settings.intake_api_keys_list
    if not accepted:
        # Empty allowlist + production = the API would silently accept anything.
        # Refuse to authenticate at all in that posture; the production-startup
        # check below makes this branch unreachable in prod.
        return None
    for candidate in accepted:
        # Constant-time comparison so timing doesn't leak which prefix matched.
        if secrets.compare_digest(candidate, token):
            return hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:16]
    return None


def _hash_body(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# ──────────────────────────────────────────────────────────────────────────
# Persistence helpers
# ──────────────────────────────────────────────────────────────────────────


async def _claim_idempotency_key(
    session: AsyncSession,
    *,
    api_key_id: str,
    idempotency_key: str,
    request_hash: str,
    request_id: str,
) -> tuple[ApiIdempotencyKey | None, JSONResponse | None]:
    """Try to insert a pending idempotency row. Returns:
    * `(row, None)` — we won the race and own the in-flight request
    * `(None, replay_response)` — a prior call already cached a response
    * `(None, conflict_response)` — same key, different body hash → 422
    """
    row = ApiIdempotencyKey(
        api_key_id=api_key_id,
        key=idempotency_key,
        request_hash=request_hash,
        state="pending",
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        # Expected race-path: uniqueness collision on (api_key_id, key).
        # Fall through to the lookup below.
        await session.rollback()
    except SQLAlchemyError:
        # F39: any non-IntegrityError DB failure means we couldn't even
        # bookkeep the idempotency claim — surface it explicitly as 500
        # rather than letting it bubble out as an opaque exception. Log
        # with exc_info so the cause is captured.
        await session.rollback()
        logger.exception(
            "idempotency_claim_db_error",
            extra={"api_key_id": api_key_id, "key": idempotency_key},
        )
        return None, _error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
            request_id=request_id,
        )
    else:
        return row, None

    # ── Collision path: look up the existing winning row. ─────────────
    existing = (
        await session.execute(
            select(ApiIdempotencyKey).where(
                ApiIdempotencyKey.api_key_id == api_key_id,
                ApiIdempotencyKey.key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        # Lost the race AND can't find the winner — extremely rare; treat
        # as transient and ask the caller to retry.
        logger.error(
            "idempotency_lookup_race",
            extra={"api_key_id": api_key_id, "key": idempotency_key},
        )
        return None, _error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
            request_id=request_id,
        )
    if existing.request_hash != request_hash:
        return None, _error_response(
            422,
            code="idempotency_key_conflict",
            request_id=request_id,
        )
    if existing.state == "completed" and existing.response_body is not None:
        return None, JSONResponse(
            status_code=existing.status_code or status.HTTP_200_OK,
            content=existing.response_body,
        )
    # Pending row from another in-flight request with the same body:
    # respond with a conservative 409 so the caller retries.
    return None, _error_response(
        status.HTTP_409_CONFLICT,
        code="idempotency_in_flight",
        request_id=request_id,
        message="A request with this Idempotency-Key is still processing.",
    )


async def _finalize_idempotency_key(
    session: AsyncSession,
    *,
    row: ApiIdempotencyKey,
    response: JSONResponse,
) -> None:
    row.state = "completed"
    row.status_code = response.status_code
    # response.body is bytes when JSONResponse has been initialized; decode it
    # back to a Python dict so the cache replay is byte-for-byte identical.
    row.response_body = _json_body_of(response)
    row.completed_at = datetime.now(UTC)


def _json_body_of(response: JSONResponse) -> dict:
    """Decode a JSONResponse's body back to a dict for cache storage."""
    import json

    return json.loads(response.body.decode("utf-8"))


# ──────────────────────────────────────────────────────────────────────────
# Domain logic shared by both intake endpoints
# ──────────────────────────────────────────────────────────────────────────


async def _resolve_contact(
    session: AsyncSession,
    *,
    email_normalized: str,
    display_name: str,
    party_kind: str,
    origin: str,
    newsletter_opt_in: bool,
    country_code: str | None,
    language_codes: list[str] | None,
    override_created_at: datetime | None = None,
    source_system: str | None = None,
    source_id: str | None = None,
) -> tuple[Contact, bool]:
    """Find an existing contact by normalized email, or create one. Returns
    (contact, created). Existing contacts are touched (display_name update
    intentionally NOT applied — the form is owner of canonical display_name
    only on first sight; preserve any staff edits made post-creation).

    ``override_created_at`` is import-only: when supplied on the insert branch,
    it becomes the new contact's ``created_at`` so historical rows don't appear
    as if they arrived today. Existing contacts keep their original timestamp.

    ``source_system`` / ``source_id`` are set only on insert (forms-etl uses
    ``jp-adopt-forms`` + the forms DB UUID for idempotency via the partial
    unique index). Live HTTP intake leaves both ``None``.
    """
    existing = (
        await session.execute(
            select(Contact).where(Contact.email_normalized == email_normalized)
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Newsletter opt-in is monotonic: once true, never silently flipped
        # back to false by a subsequent submission that omits the checkbox.
        if newsletter_opt_in and not existing.newsletter_opt_in:
            existing.newsletter_opt_in = True
        return existing, False

    initial_status = "new" if party_kind == "adopter" else None
    initial_fac_status = "new" if party_kind == "facilitator" else None
    contact_kwargs: dict[str, object] = {
        "id": uuid.uuid4(),
        "party_kind": party_kind,
        "display_name": display_name,
        "adopter_status": initial_status,
        "facilitator_status": initial_fac_status,
        "email_normalized": email_normalized,
        "origin": origin,
        "newsletter_opt_in": newsletter_opt_in,
        "country_code": country_code,
        "language_codes": language_codes,
    }
    if override_created_at is not None:
        contact_kwargs["created_at"] = override_created_at
    if source_system is not None:
        contact_kwargs["source_system"] = source_system
    if source_id is not None:
        contact_kwargs["source_id"] = source_id
    contact = Contact(**contact_kwargs)
    session.add(contact)
    await session.flush()
    return contact, True


async def _apply_profile(
    session: AsyncSession,
    contact: Contact,
    profile_in: ContactProfileIntake | None,
) -> None:
    """U10: upsert the 1:1 contact_profile from the submitted profile fields.
    Only fields the form actually sent are written (exclude_unset)."""
    if profile_in is None:
        return
    data = profile_in.model_dump(exclude_unset=True)
    if not data:
        return
    prof = (
        await session.execute(
            select(ContactProfile).where(ContactProfile.contact_id == contact.id)
        )
    ).scalar_one_or_none()
    if prof is None:
        prof = ContactProfile(id=uuid.uuid4(), contact_id=contact.id)
        session.add(prof)
    for field_name, value in data.items():
        setattr(prof, field_name, value)


async def _write_consents(
    session: AsyncSession,
    contact: Contact,
    consents: list[ConsentIn],
) -> None:
    """U10: persist MOU (or future) consent acceptances sent with the form."""
    for c in consents:
        session.add(
            Consent(
                id=uuid.uuid4(),
                contact_id=contact.id,
                consent_type=c.consent_type,
                version=c.version,
                content_hash=c.content_hash,
                accepted_at=c.accepted_at,
                conversation_id=c.conversation_id,
                evidence=c.evidence,
            )
        )


async def _unknown_people_id3s(
    session: AsyncSession, selections: list[FpgInterestIn]
) -> list[str]:
    """Return people_id3 codes not present in ``fpg`` (empty when all known)."""
    codes = sorted({str(sel.people_id3).strip() for sel in selections})
    if not codes:
        return []
    found = set(
        (
            await session.execute(
                select(Fpg.people_id3).where(Fpg.people_id3.in_(codes))
            )
        ).scalars().all()
    )
    return sorted(set(codes) - found)


async def _create_interests(
    session: AsyncSession,
    contact: Contact,
    selections: list[FpgInterestIn],
) -> list[uuid.UUID]:
    """Create one AdopterInterest row per FPG selection (shared by the adopter
    and facilitator intake paths). The per-FPG facilitation_services /
    network_services / engagement_status columns (U7) carry the facilitator
    answers; commitment_* carry the adopter answers."""
    interest_ids: list[uuid.UUID] = []
    for sel in selections:
        interest = AdopterInterest(
            id=uuid.uuid4(),
            contact_id=contact.id,
            people_id3=str(sel.people_id3),
            commitment_level=sel.commitment_level,
            notes=sel.notes,
            commitment_types=sel.commitment_types,
            engagement_status=sel.engagement_status,
            facilitation_services=sel.facilitation_services,
            network_services=sel.network_services,
        )
        session.add(interest)
        await session.flush()
        interest_ids.append(interest.id)
    return interest_ids


async def process_adoption_payload(
    session: AsyncSession,
    *,
    payload: AdoptionIntake,
    settings: Settings,
    request_id: str | None = None,
    override_created_at: datetime | None = None,
    source_system: str | None = None,
    source_id: str | None = None,
) -> IntakeOutcome:
    """Canonical adoption intake logic. Raises :class:`IntakeValidationError`
    on validation failures; returns :class:`IntakeOutcome` on success or when
    the contact is blocked (anti-enumeration)."""
    email_normalized = normalize_email(payload.email)
    if not _EMAIL_SANITY.match(email_normalized):
        raise IntakeValidationError(
            fields={"email": ["Email failed sanity check"]},
        )

    contact, created = await _resolve_contact(
        session,
        email_normalized=email_normalized,
        display_name=payload.display_name,
        party_kind="adopter",
        origin=payload.origin or settings.intake_default_origin,
        newsletter_opt_in=payload.newsletter_opt_in,
        country_code=payload.country_code,
        language_codes=payload.language_codes,
        override_created_at=override_created_at,
        source_system=source_system,
        source_id=source_id,
    )

    if contact.adopter_status == "do_not_engage":
        # Anti-enumeration: log, return success-shaped response with NO
        # submission written so the caller can't probe blocklist membership.
        #
        # N1: must return 201 (matching the accepted-first-call status) so the
        # status code itself doesn't reveal that the contact is blocked. F14
        # introduced 201 for first-successful processing; if we return 200 here
        # the blocked path becomes a deterministic do_not_engage oracle
        # (201 = real, 200 = blocked).
        #
        # N1 body-shape oracle: accepted submissions always return
        # ``len(interestIds) >= 1`` (one row per fpg_selection, or one synthetic
        # row when fpg_selections is empty per the potential_adopter path).
        # A blocked response that returned ``interestIds=[]`` would therefore
        # leak blocklist membership through the response body even after the
        # status-code parity fix. Fabricate ephemeral UUIDs to mirror the
        # accepted-shape — they are NEVER persisted (no AdopterInterest row is
        # written for blocked contacts) and used only to defeat the body-shape
        # oracle.
        #
        # F15: persist only a PII-light fingerprint of the submission rather
        # than the raw form payload. Storing the full body indefinitely is a
        # GDPR/retention liability — the operator-visible audit only needs
        # enough to recognize the blocked attempt. A future cleanup task
        # should also apply a TTL purge (~90d) to this table.
        session.add(
            SubmissionBlocked(
                contact_id=contact.id,
                email_normalized=email_normalized,
                reason="do_not_engage",
                source="adoption_intake",
                submission_payload={
                    "email_normalized": email_normalized,
                    "party_kind": payload.party_kind,
                    "received_at": datetime.now(UTC).isoformat(),
                },
            )
        )
        # Mirror the accepted-path interest_ids LENGTH so body shape is
        # indistinguishable: one synthetic id per fpg_selection, or one when the
        # caller submitted no selections (matching the no_fpg branch below).
        fabricated_interest_ids = [
            uuid.uuid4() for _ in (payload.fpg_selections or [None])
        ]
        return IntakeOutcome(
            contact_id=contact.id,
            created=created,
            interest_ids=fabricated_interest_ids,
            was_blocked=True,
            submission_id=uuid.uuid4(),
        )

    # Multi-FPG: one Contact + N AdopterInterest rows. Empty list → mark
    # contact as `potential_adopter` (R2: wants help selecting), insert ONE
    # interest with people_id3=NULL so downstream matching has a record to triage.
    interest_ids: list[uuid.UUID] = []
    if not payload.fpg_selections:
        contact.adopter_status = "potential_adopter"
        no_fpg = AdopterInterest(
            id=uuid.uuid4(),
            contact_id=contact.id,
            people_id3=None,
            commitment_level=None,
            notes=None,
        )
        session.add(no_fpg)
        await session.flush()
        interest_ids.append(no_fpg.id)
    else:
        missing = await _unknown_people_id3s(session, payload.fpg_selections)
        if missing:
            raise IntakeValidationError(
                message=f"Unknown people_id3 codes: {missing}",
                fields={
                    "fpg_selections": [
                        f"Unknown people_id3: {code}" for code in missing
                    ]
                },
            )
        interest_ids = await _create_interests(
            session, contact, payload.fpg_selections
        )

    # U10: persist the submitted adoption profile + consent records.
    await _apply_profile(session, contact, payload.profile)
    await _write_consents(session, contact, payload.consents)

    submission_id = uuid.uuid4()
    outbox_payload = {
        "event": EVENT_SUBMISSION_RECEIVED,
        "schema_version": "jp.adopt.v2",
        "submission_id": str(submission_id),
        "request_id": request_id,
        "contact_id": str(contact.id),
        "contact_created": created,
        "party_kind": "adopter",
        "interest_ids": [str(i) for i in interest_ids],
        "fpg_selections": [s.model_dump() for s in payload.fpg_selections],
        "origin": contact.origin,
        "newsletter_opt_in": contact.newsletter_opt_in,
    }
    emit_outbox(
        session,
        event_type=EVENT_SUBMISSION_RECEIVED,
        payload=outbox_payload,
    )

    # A completed form sign-up enters the adopter funnel as a real submission,
    # so it should receive the "Adopter sign-up welcome" drip — which triggers
    # on ``contact.submitted`` (the same event a draft→new promotion emits).
    # Forms intake lands the contact directly in 'new'/'potential_adopter'
    # without going through that transition, so emit the event here too.
    # Only on first creation: a returning submitter (created=False) is already
    # in the funnel and must not be re-welcomed. do_not_engage contacts return
    # earlier and never reach here. During bulk forms-etl import emit_outbox is
    # suppressed, so historical signups don't retroactively enroll.
    if created:
        emit_outbox(
            session,
            event_type=EVENT_CONTACT_SUBMITTED,
            payload={
                "event": EVENT_CONTACT_SUBMITTED,
                "contact_id": str(contact.id),
                "party_kind": "adopter",
                "adopter_status": contact.adopter_status,
            },
        )

    return IntakeOutcome(
        contact_id=contact.id,
        created=created,
        interest_ids=interest_ids,
        was_blocked=False,
        submission_id=submission_id,
    )


async def _process_adoption(
    session: AsyncSession,
    *,
    payload: AdoptionIntake,
    settings: Settings,
    request_id: str,
) -> JSONResponse:
    try:
        outcome = await process_adoption_payload(
            session, payload=payload, settings=settings, request_id=request_id
        )
    except IntakeValidationError as exc:
        return _error_response(
            status.HTTP_400_BAD_REQUEST,
            code=exc.code,
            request_id=request_id,
            message=exc.message,
            fields=exc.fields,
        )
    return _success_response(
        status.HTTP_201_CREATED,
        submission_id=outcome.submission_id,
        request_id=request_id,
        contact_id=outcome.contact_id,
        interest_ids=outcome.interest_ids,
    )


async def process_facilitation_payload(
    session: AsyncSession,
    *,
    payload: FacilitationIntake,
    settings: Settings,
    request_id: str | None = None,
    override_created_at: datetime | None = None,
    source_system: str | None = None,
    source_id: str | None = None,
) -> IntakeOutcome:
    """Canonical facilitation intake logic. See :func:`process_adoption_payload`."""
    email_normalized = normalize_email(payload.email)
    if not _EMAIL_SANITY.match(email_normalized):
        raise IntakeValidationError(
            fields={"email": ["Email failed sanity check"]},
        )

    contact, created = await _resolve_contact(
        session,
        email_normalized=email_normalized,
        display_name=payload.display_name,
        party_kind="facilitator",
        origin=payload.origin or settings.intake_default_origin,
        newsletter_opt_in=payload.newsletter_opt_in,
        country_code=payload.country_code,
        language_codes=payload.language_codes,
        override_created_at=override_created_at,
        source_system=source_system,
        source_id=source_id,
    )

    if contact.facilitator_status == "do_not_engage":
        # N1: return 201 (not 200) so status code doesn't act as a
        # do_not_engage oracle. See the adoption side for the full
        # anti-enumeration rationale.
        # See F15 note above on the adoption side: only the minimal
        # fingerprint is persisted (not the raw payload).
        session.add(
            SubmissionBlocked(
                contact_id=contact.id,
                email_normalized=email_normalized,
                reason="do_not_engage",
                source="facilitation_intake",
                submission_payload={
                    "email_normalized": email_normalized,
                    "party_kind": payload.party_kind,
                    "received_at": datetime.now(UTC).isoformat(),
                },
            )
        )
        # N1 body-shape oracle (U12): now that an accepted facilitation
        # response carries one interest id per fpg_selection, a blocked
        # response with interest_ids=[] would leak blocklist membership when
        # the caller submitted selections. Fabricate ephemeral (never
        # persisted) ids matching the submitted count to keep the shape
        # indistinguishable. Empty selections → [] on both paths, so no
        # forced synthetic row here (unlike the adoption potential_adopter
        # path, which always writes one).
        fabricated_interest_ids = [uuid.uuid4() for _ in payload.fpg_selections]
        return IntakeOutcome(
            contact_id=contact.id,
            created=created,
            interest_ids=fabricated_interest_ids,
            was_blocked=True,
            submission_id=uuid.uuid4(),
        )

    # U12: facilitators select FPGs too — one AdopterInterest row per pick,
    # with the facilitation_services / network_services / engagement_status
    # columns carrying the per-FPG answers.
    missing = await _unknown_people_id3s(session, payload.fpg_selections)
    if missing:
        raise IntakeValidationError(
            message=f"Unknown people_id3 codes: {missing}",
            fields={
                "fpg_selections": [
                    f"Unknown people_id3: {code}" for code in missing
                ]
            },
        )
    interest_ids = await _create_interests(session, contact, payload.fpg_selections)

    # U10: persist the submitted profile + consent records (facilitation).
    await _apply_profile(session, contact, payload.profile)
    await _write_consents(session, contact, payload.consents)

    submission_id = uuid.uuid4()
    outbox_payload = {
        "event": EVENT_SUBMISSION_RECEIVED,
        "schema_version": "jp.adopt.v2",
        "submission_id": str(submission_id),
        "request_id": request_id,
        "contact_id": str(contact.id),
        "contact_created": created,
        "party_kind": "facilitator",
        "organization_name": payload.organization_name,
        "interest_ids": [str(i) for i in interest_ids],
        "fpg_selections": [s.model_dump() for s in payload.fpg_selections],
        "origin": contact.origin,
        "newsletter_opt_in": contact.newsletter_opt_in,
    }
    emit_outbox(
        session,
        event_type=EVENT_SUBMISSION_RECEIVED,
        payload=outbox_payload,
    )

    return IntakeOutcome(
        contact_id=contact.id,
        created=created,
        interest_ids=interest_ids,
        was_blocked=False,
        submission_id=submission_id,
    )


async def _process_facilitation(
    session: AsyncSession,
    *,
    payload: FacilitationIntake,
    settings: Settings,
    request_id: str,
) -> JSONResponse:
    try:
        outcome = await process_facilitation_payload(
            session, payload=payload, settings=settings, request_id=request_id
        )
    except IntakeValidationError as exc:
        return _error_response(
            status.HTTP_400_BAD_REQUEST,
            code=exc.code,
            request_id=request_id,
            message=exc.message,
            fields=exc.fields,
        )
    return _success_response(
        status.HTTP_201_CREATED,
        submission_id=outcome.submission_id,
        request_id=request_id,
        contact_id=outcome.contact_id,
        interest_ids=outcome.interest_ids,
    )


# ──────────────────────────────────────────────────────────────────────────
# Request orchestration shared between both endpoints
# ──────────────────────────────────────────────────────────────────────────


async def _read_raw_body(
    request: Request, request_id: str
) -> tuple[bytes, JSONResponse | None]:
    raw = await request.body()
    if not raw:
        return raw, _error_response(
            status.HTTP_400_BAD_REQUEST,
            code="validation_failed",
            request_id=request_id,
            fields={"_root": ["Empty request body"]},
        )
    if len(raw) > INTAKE_MAX_BODY_BYTES:
        # 413 was renamed in newer starlette; use the numeric literal so we
        # don't trip the deprecation warning on either spelling.
        return raw, _error_response(
            413,
            code="payload_too_large",
            request_id=request_id,
            maxBytes=INTAKE_MAX_BODY_BYTES,
        )
    return raw, None


def _parse_json(
    raw: bytes, request_id: str
) -> tuple[object | None, JSONResponse | None]:
    import json

    try:
        return json.loads(raw.decode("utf-8")), None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, _error_response(
            status.HTTP_400_BAD_REQUEST,
            code="validation_failed",
            request_id=request_id,
            fields={"_root": ["Malformed JSON"]},
        )


def _zod_like_errors(exc) -> dict[str, list[str]]:  # pyright: ignore[reportMissingTypeStub]
    """Convert Pydantic ValidationError into the same shape jp-adopt-forms
    emits from its Zod-error mapper. Keys are dotted field paths; values are
    arrays of message strings."""
    out: dict[str, list[str]] = {}
    for err in exc.errors():
        path = ".".join(str(p) for p in err["loc"]) or "_root"
        out.setdefault(path, []).append(err["msg"])
    return out


async def _handle(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    *,
    authorization: str | None,
    idempotency_key: str | None,
    domain: str,
) -> Response:
    request_id = _request_id()

    # Production startup guard: refuse to accept any submission when no key
    # is configured (env-var allowlist empty AND no active DB key), no
    # matter the bearer the caller sent. With #59's self-managed keys,
    # we now check both sources before returning 503 — staff can mint
    # one through the admin UI to bring intake back online without an
    # infra-team round-trip.
    if settings.is_production and not settings.intake_api_keys_list:
        from jp_adopt_api.models import IntakeApiKey

        active_db_key_count = (
            await session.execute(
                select(func.count(IntakeApiKey.id)).where(
                    IntakeApiKey.revoked_at.is_(None)
                )
            )
        ).scalar_one()
        if not active_db_key_count:
            logger.error("intake_no_api_keys_configured_in_production")
            return _error_response(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                code="intake_disabled",
                request_id=request_id,
                message="Intake endpoint is not configured.",
            )

    api_key_id = await _authenticate(authorization, settings, session, request)
    if api_key_id is None:
        return _error_response(
            status.HTTP_401_UNAUTHORIZED,
            code="unauthorized",
            request_id=request_id,
        )

    if not idempotency_key:
        return _error_response(
            status.HTTP_400_BAD_REQUEST,
            code="idempotency_required",
            request_id=request_id,
        )

    raw, err = await _read_raw_body(request, request_id)
    if err is not None:
        return err

    parsed_json, err = _parse_json(raw, request_id)
    if err is not None:
        return err

    schema = AdoptionIntake if domain == "adoption" else FacilitationIntake
    try:
        payload = schema.model_validate(parsed_json)
    except Exception as e:  # noqa: BLE001 — Pydantic ValidationError handled by attr
        from pydantic import ValidationError

        if isinstance(e, ValidationError):
            return _error_response(
                status.HTTP_400_BAD_REQUEST,
                code="validation_failed",
                request_id=request_id,
                fields=_zod_like_errors(e),
            )
        raise

    request_hash = _hash_body(raw)
    idem_row, replay = await _claim_idempotency_key(
        session,
        api_key_id=api_key_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        request_id=request_id,
    )
    if replay is not None:
        # Idempotent replay or conflict path: nothing else to write. The
        # claim attempt rolled back any partial state.
        return replay
    assert idem_row is not None  # mypy / pyright happiness

    if domain == "adoption":
        assert isinstance(payload, AdoptionIntake)
        response = await _process_adoption(
            session, payload=payload, settings=settings, request_id=request_id
        )
    else:
        assert isinstance(payload, FacilitationIntake)
        response = await _process_facilitation(
            session, payload=payload, settings=settings, request_id=request_id
        )

    await _finalize_idempotency_key(session, row=idem_row, response=response)
    await session.commit()
    return response


# ──────────────────────────────────────────────────────────────────────────
# Endpoint definitions
# ──────────────────────────────────────────────────────────────────────────


# Shared OpenAPI ``responses`` map for both intake endpoints. The handler
# returns a JSONResponse directly (so it can control status codes precisely),
# but FastAPI documents the bodies via these per-status entries. Generated
# clients (``pnpm contracts:generate``) get a typed envelope for each
# outcome including the error envelope.
_INTAKE_RESPONSES: dict[int | str, dict[str, object]] = {
    200: {"model": IntakeSuccess, "description": "Idempotent replay (cached)"},
    201: {"model": IntakeSuccess, "description": "First successful processing"},
    400: {"model": IntakeError, "description": "validation / idempotency_required"},
    401: {"model": IntakeError, "description": "Bearer missing or unknown"},
    409: {"model": IntakeError, "description": "Idempotency-Key in-flight"},
    413: {"model": IntakeError, "description": "payload_too_large"},
    422: {"model": IntakeError, "description": "idempotency_key_conflict"},
    503: {"model": IntakeError, "description": "intake_disabled (no keys)"},
}


# AC-10: ``Idempotency-Key`` is required by the server (handler returns 400
# ``idempotency_required`` when missing) but FastAPI's auto-generated OpenAPI
# marks it ``required: false`` because the param is typed ``str | None``.
# Generated clients then don't send it and hit the 400 at runtime. The
# ``main.py`` custom-openapi function post-processes this flag to ``true``
# on the two intake operations.
# AC-13: also declare the IntakeBearerKey security requirement on each
# intake operation so generated clients auto-inject the Authorization header.
_INTAKE_OPENAPI_EXTRA: dict[str, object] = {
    "security": [{"IntakeBearerKey": []}],
}


@router.post(
    "/adoption",
    response_model=IntakeSuccess,
    responses=_INTAKE_RESPONSES,
    openapi_extra=_INTAKE_OPENAPI_EXTRA,
)
async def post_adoption_intake(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    return await _handle(
        request,
        db,
        settings,
        authorization=authorization,
        idempotency_key=idempotency_key,
        domain="adoption",
    )


@router.post(
    "/facilitation",
    response_model=IntakeSuccess,
    responses=_INTAKE_RESPONSES,
    openapi_extra=_INTAKE_OPENAPI_EXTRA,
)
async def post_facilitation_intake(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    return await _handle(
        request,
        db,
        settings,
        authorization=authorization,
        idempotency_key=idempotency_key,
        domain="facilitation",
    )
