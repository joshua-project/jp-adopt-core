from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

# ── Adoption profile (U9): contact_profile field enums ─────────────────────
# Option sets mirror dt-adoption-fields/custom-fields.php + migration 0012.
EntitySize = Literal["1", "lt_30", "31_100", "101_500", "501_2000", "2001_plus"]
AdopterType = Literal[
    "individual", "small_group", "church", "organization", "network"
]
MouStatus = Literal["signed", "not_required", "not_sent"]
PreferredCommunication = Literal["email", "phone"]


class ContactProfileRead(BaseModel):
    """Read view of the 1:1 contact_profile (the JP-custom adoption fields).
    Enum-shaped fields are typed ``str`` here so stored values always
    round-trip even if the option set later changes."""

    model_config = ConfigDict(from_attributes=True)

    ministry_areas: list[str] | None = None
    entity_size: str | None = None
    primary_contact_name: str | None = None
    secondary_contact_name: str | None = None
    secondary_contact_email: str | None = None
    secondary_contact_phone: str | None = None
    website: str | None = None
    preferred_communication: str | None = None
    form_country: str | None = None
    form_state_region: str | None = None
    adopter_type: str | None = None
    commitment_types: list[str] | None = None
    commitment_date: date | None = None
    works_with_fpgs: bool | None = None
    willing_to_facilitate: bool | None = None
    facilitation_entity_types: list[str] | None = None
    facilitation_entity_sizes: list[str] | None = None
    mou_status: str | None = None
    mou_signature_name: str | None = None
    want_facilitator_connection: bool | None = None
    facilitator_entity_types: list[str] | None = None
    desired_facilitator_info: list[str] | None = None
    want_network_connection: bool | None = None
    network_partner_info: list[str] | None = None
    has_doctrinal_distinctives: bool | None = None
    doctrinal_distinctives: str | None = None
    has_accountability_membership: bool | None = None
    accountability_memberships: str | None = None
    last_contact_date: date | None = None
    engagement_score: int | None = None
    next_followup_date: date | None = None
    referral_source: str | None = None
    campaign: str | None = None
    partner: str | None = None
    additional_notes: str | None = None
    file_download_url: str | None = None


class ContactProfilePatch(BaseModel):
    """Editable subset of the profile. Enum fields are validated here so a bad
    value is a 422, not a DB CHECK 500. ``referral_source`` / ``campaign`` /
    ``partner`` / ``file_download_url`` are set at intake and intentionally
    NOT patchable. Status stays transition-only (never here)."""

    model_config = ConfigDict(extra="forbid")

    ministry_areas: list[str] | None = None
    entity_size: EntitySize | None = None
    primary_contact_name: str | None = Field(default=None, max_length=512)
    secondary_contact_name: str | None = Field(default=None, max_length=512)
    secondary_contact_email: str | None = Field(default=None, max_length=512)
    secondary_contact_phone: str | None = Field(default=None, max_length=128)
    website: str | None = Field(default=None, max_length=1024)
    preferred_communication: PreferredCommunication | None = None
    form_country: str | None = Field(default=None, max_length=128)
    form_state_region: str | None = Field(default=None, max_length=128)
    adopter_type: AdopterType | None = None
    commitment_types: list[str] | None = None
    commitment_date: date | None = None
    works_with_fpgs: bool | None = None
    willing_to_facilitate: bool | None = None
    facilitation_entity_types: list[str] | None = None
    facilitation_entity_sizes: list[str] | None = None
    mou_status: MouStatus | None = None
    mou_signature_name: str | None = Field(default=None, max_length=512)
    want_facilitator_connection: bool | None = None
    facilitator_entity_types: list[str] | None = None
    desired_facilitator_info: list[str] | None = None
    want_network_connection: bool | None = None
    network_partner_info: list[str] | None = None
    has_doctrinal_distinctives: bool | None = None
    doctrinal_distinctives: str | None = Field(default=None, max_length=4096)
    has_accountability_membership: bool | None = None
    accountability_memberships: str | None = Field(default=None, max_length=4096)
    last_contact_date: date | None = None
    engagement_score: int | None = Field(default=None, ge=0, le=100)
    next_followup_date: date | None = None
    additional_notes: str | None = Field(default=None, max_length=4096)


class ContactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    party_kind: str
    display_name: str
    adopter_status: str | None
    facilitator_status: str | None
    # F21: U1 added several Contact columns that clients need; the previous
    # ContactRead omitted them, forcing the UI to re-query or guess. Internal
    # fields (source_system, source_id, b2c_subject_id,
    # local_modified_after_import) are intentionally NOT exposed.
    version: int
    email_normalized: str | None
    country_code: str | None
    language_codes: list[str] | None
    origin: str | None
    newsletter_opt_in: bool
    created_at: datetime
    updated_at: datetime
    # U9: 1:1 adoption profile (null when the contact has no profile row yet).
    profile: ContactProfileRead | None = None
    # U13: assigned staff owner's subject (null = unassigned). Populated on the
    # single-contact read, not the list.
    assigned_to: str | None = None


class ContactAssignmentRequest(BaseModel):
    """Assign a contact to a staff user. ``user_subject_id`` omitted → assign to
    the calling user (the common 'assign to me' case)."""

    model_config = ConfigDict(extra="forbid")

    user_subject_id: str | None = Field(default=None, min_length=1, max_length=255)


class ContactListResponse(BaseModel):
    items: list[ContactRead]
    total: int
    limit: int
    offset: int


# Pipeline status-count surface used by the staff /adopters and /facilitators
# pages to render filter-chip badges. Returns one of two shapes depending on
# the requested ``party_kind``:
#
#   {"party_kind": "adopter",     "counts": {"new": 5, "matched": 3, ...}}
#   {"party_kind": "facilitator", "counts": {"new": 2, "ready": 1, ...}}
#
# NULL statuses are aggregated under the synthetic key ``__unset__`` so the
# client can decide whether to show them as a separate chip or hide them.
class ContactStatusCounts(BaseModel):
    party_kind: Literal["adopter", "facilitator"]
    counts: dict[str, int]
    total: int


class ContactPatch(BaseModel):
    # F5 of the U1-U6 review: ``adopter_status`` and ``facilitator_status``
    # were intentionally REMOVED from the patch surface so a generic PATCH
    # cannot bypass the state-machine's role/reason-code/audit guarantees.
    # A dedicated ``POST /v1/contacts/{id}/transition`` endpoint (U7) is the
    # only supported path for status mutations. Free-form display fields
    # remain editable here.
    #
    # N2: ``extra='forbid'`` so a client that still POSTs the removed
    # ``adopter_status`` / ``facilitator_status`` keys gets a 422 with the
    # offending field named, rather than a silent 200 that drops the field
    # (which would hide a real bypass-attempt from operators).
    model_config = ConfigDict(extra="forbid")

    party_kind: str | None = Field(default=None, min_length=1, max_length=64)
    display_name: str | None = Field(default=None, min_length=1, max_length=512)
    # U9: adoption-profile edits upsert the 1:1 contact_profile row. This does
    # NOT touch Contact.version (the optimistic-lock column the match/transition
    # flows gate on) — profile churn stays off the hot contact row.
    profile: ContactProfilePatch | None = None

    @model_validator(mode="after")
    def reject_null_for_non_nullable_columns(self) -> Self:
        """Reject null for NOT NULL columns (422, not IntegrityError)."""
        for fname in ("party_kind", "display_name"):
            if fname in self.model_fields_set and getattr(self, fname) is None:
                raise ValueError(f"{fname} cannot be null")
        return self


# ── Contact record (U1): per-contact aggregates for /contacts/[id] ─────────
#
# These power the canonical contact-record page. They surface data already
# stored (matches, transition_audit, activity_log) that previously had no read
# surface. Raw enum values are returned as-is; the web client humanizes via
# apps/web/src/lib/vocab.ts.


class ContactMatchRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    adopter_interest_id: uuid.UUID
    people_id3: str | None
    people_id3_name: str | None = None
    people_id3_country: str | None = None
    facilitator_org_id: uuid.UUID
    facilitator_name: str
    status: str
    recommended_at: datetime
    decided_at: datetime | None
    decided_by: str | None
    decision_reason_code: str | None
    decision_reason_text: str | None


class ContactMatchesResponse(BaseModel):
    items: list[ContactMatchRow]
    total: int


class ContactTransitionRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    from_state: str | None
    to_state: str
    actor_id: str | None
    actor_role: str | None
    reason_code: str | None
    reason_text: str | None
    occurred_at: datetime


class ContactTransitionsResponse(BaseModel):
    items: list[ContactTransitionRow]
    total: int


class ContactActivityRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    author_id: str
    body: str
    kind: str | None
    occurred_at: datetime
    created_at: datetime


class ContactActivityResponse(BaseModel):
    items: list[ContactActivityRow]
    total: int


class ContactEnrollmentEventRow(BaseModel):
    """One enrollment_event entry surfaced to the per-contact drips panel."""

    model_config = ConfigDict(from_attributes=True)

    event_type: str
    payload: dict[str, Any] | None = None
    created_at: datetime


class ContactEnrollmentRow(BaseModel):
    """One drip-campaign enrollment for the contact (the #55 read slice)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    campaign_id: uuid.UUID
    campaign_name: str
    state: str
    current_step_position: int
    enrolled_at: datetime
    last_step_sent_at: datetime | None
    exit_reason: str | None
    # Most-recent events first, capped per enrollment server-side so the
    # response stays bounded even when an enrollment accumulates many.
    events: list[ContactEnrollmentEventRow] = Field(default_factory=list)


class ContactEnrollmentsResponse(BaseModel):
    items: list[ContactEnrollmentRow]
    total: int


class ContactTimelineEntry(BaseModel):
    """One merged feed entry. ``type`` discriminates the source table so the
    UI can pick an icon; ``ref_id`` is the source row id as a string."""

    type: Literal["transition", "match", "activity"]
    at: datetime
    title: str
    detail: str | None = None
    ref_id: str


class ContactTimelineResponse(BaseModel):
    items: list[ContactTimelineEntry]


# ── Add-note (U2): write a staff note into activity_log ────────────────────


class ContactNoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=8192)
    kind: str | None = Field(default="note", max_length=64)


class ContactEmailCreate(BaseModel):
    """F3: staff-composed email to a contact. Recorded as an ``email`` note."""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=512)
    body: str = Field(min_length=1, max_length=16384)
    # Facilitators only: also send to the org's secondary contact email when
    # one is on file. Ignored for adopters (no secondary contact).
    include_secondary: bool = False


class ContactEmailResponse(BaseModel):
    note_id: uuid.UUID
    to: list[str]
    # Send status of the recorded note at response time. The actual ACS send
    # happens in a background task; the note's stored status flips to
    # ``sent`` / ``failed`` once it resolves.
    status: str


# ── Intake (U4): Form A facilitation + Form B adoption ─────────────────────
#
# These schemas mirror jp-adopt-forms' POST envelope and accept the subset of
# form fields jp-adopt-core needs to (a) dedupe the contact, (b) record
# intent, (c) emit a `submission.received` outbox event for downstream matching.
# Forms-app retains the full payload; we don't try to round-trip its complete
# shape. Unknown fields in the body are ignored (no extra="forbid") so the
# forms-app contract can grow without breaking us.


ORIGIN_VALUES = (
    "core_org",
    "website",
    "third_party_referral",
    "partner_event",
    "manual_entry",
    "other",
)
PartyKindIntake = Literal["adopter", "facilitator"]


class FpgInterestIn(BaseModel):
    """One row of the adopter's FPG selection on Form B.

    ``people_id3`` is the canonical Joshua Project people-group ID in core.
    Empty list at the request level → adopter is ``potential_adopter``.
    """

    model_config = ConfigDict(extra="ignore")

    people_id3: str
    commitment_level: str | None = Field(default=None, max_length=64)

    @field_validator("people_id3", mode="before")
    @classmethod
    def _coerce_people_id3(cls, value: object) -> str:
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError("people_id3 is required")
        return str(value).strip()
    notes: str | None = Field(default=None, max_length=2048)
    # U10: per-FPG answers from the forms → adopter_interest (U7 columns).
    commitment_types: list[str] | None = None
    engagement_status: str | None = Field(default=None, max_length=32)
    facilitation_services: list[str] | None = None
    network_services: list[str] | None = None


class ContactProfileIntake(ContactProfilePatch):
    """Profile fields accepted at intake. Lenient (ignores unknown form keys)
    and — unlike the staff PATCH surface — includes the submission-derived
    fields (referral_source / campaign / partner / file_download_url) that are
    readonly once set."""

    model_config = ConfigDict(extra="ignore")

    referral_source: str | None = Field(default=None, max_length=512)
    campaign: str | None = Field(default=None, max_length=512)
    partner: str | None = Field(default=None, max_length=512)
    file_download_url: str | None = Field(default=None, max_length=2048)


class ConsentIn(BaseModel):
    """An MOU (or future) consent acceptance sent with a submission."""

    model_config = ConfigDict(extra="ignore")

    consent_type: str = Field(max_length=64)
    version: str = Field(max_length=64)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_at: datetime
    conversation_id: str | None = Field(default=None, max_length=128)
    evidence: dict[str, Any] | None = None


class IntakeBase(BaseModel):
    model_config = ConfigDict(extra="ignore")

    email: EmailStr
    display_name: str = Field(min_length=1, max_length=512)
    origin: str | None = Field(default=None, max_length=64)
    newsletter_opt_in: bool = False
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    language_codes: list[str] | None = None
    # Free-form bag for forms-app fields we don't model individually but want
    # to preserve verbatim in the outbox event payload + (eventually) in a
    # raw_submission audit table.
    extra: dict[str, Any] | None = None
    # U10: structured adoption profile + consent records the forms now send.
    profile: ContactProfileIntake | None = None
    consents: list[ConsentIn] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def normalize_strings(self) -> Self:
        if self.country_code:
            object.__setattr__(self, "country_code", self.country_code.upper())
        if self.language_codes:
            object.__setattr__(
                self,
                "language_codes",
                [c.strip().lower() for c in self.language_codes if c.strip()],
            )
        if self.origin and self.origin not in ORIGIN_VALUES:
            # Loose validation: log-and-pass would be wrong here because the
            # contact lands as the origin we record. Reject unknown values so
            # forms-app must opt in to taxonomy changes.
            raise ValueError(
                f"origin must be one of {ORIGIN_VALUES}, got {self.origin!r}"
            )
        return self


class AdoptionIntake(IntakeBase):
    """Form B (`/adopt`) payload: an adopter, possibly multi-FPG."""

    party_kind: Literal["adopter"] = "adopter"
    # adv4-001 / #87: bound the FPG selection list so the A1 fabrication path
    # can't be driven to allocate unbounded UUIDs. Originally 20, but real
    # high-coverage submissions (Mission India's 1,701-FPG facilitation, plus
    # five others) legitimately exceed it. Raised to 2000 after product review;
    # still well below abuse, and INTAKE_MAX_BODY_BYTES caps the absolute body.
    fpg_selections: list[FpgInterestIn] = Field(default_factory=list, max_length=2000)


class FacilitationIntake(IntakeBase):
    """Form A (`/facilitate-adoption`) payload: a facilitator (org or person)."""

    party_kind: Literal["facilitator"] = "facilitator"
    organization_name: str | None = Field(default=None, max_length=512)
    # U12: facilitators also pick FPGs (which groups they can serve, with
    # per-FPG engagement_status / facilitation_services / network_services).
    # Same shape + bound as the adoption side (2000, raised from 20 per #87);
    # an empty list is fine (a facilitator with no specific FPGs yet).
    fpg_selections: list[FpgInterestIn] = Field(default_factory=list, max_length=2000)


class IntakeSuccessData(BaseModel):
    submission_id: uuid.UUID = Field(serialization_alias="submissionId")
    request_id: str = Field(serialization_alias="requestId")
    contact_id: uuid.UUID = Field(serialization_alias="contactId")
    interest_ids: list[uuid.UUID] = Field(serialization_alias="interestIds")

    model_config = ConfigDict(populate_by_name=True)


class IntakeSuccess(BaseModel):
    api_version: str = Field(default="1", serialization_alias="apiVersion")
    ok: Literal[True] = True
    data: IntakeSuccessData

    model_config = ConfigDict(populate_by_name=True)


class IntakeErrorBody(BaseModel):
    code: str
    message: str | None = None
    fields: dict[str, list[str]] | None = None
    request_id: str | None = Field(default=None, serialization_alias="requestId")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class IntakeError(BaseModel):
    api_version: str = Field(default="1", serialization_alias="apiVersion")
    ok: Literal[False] = False
    error: IntakeErrorBody

    model_config = ConfigDict(populate_by_name=True)
