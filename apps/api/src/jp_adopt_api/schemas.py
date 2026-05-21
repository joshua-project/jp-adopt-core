from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


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

    @model_validator(mode="after")
    def reject_null_for_non_nullable_columns(self) -> Self:
        """Reject null for NOT NULL columns (422, not IntegrityError)."""
        for fname in ("party_kind", "display_name"):
            if fname in self.model_fields_set and getattr(self, fname) is None:
                raise ValueError(f"{fname} cannot be null")
        return self


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

    `rop3` is the canonical Joshua Project people-group ID. Empty list at the
    request level → adopter is `potential_adopter` (wants help selecting).
    """

    model_config = ConfigDict(extra="ignore")

    rop3: str = Field(min_length=1, max_length=32)
    commitment_level: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2048)


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
    # adv4-001: bound the FPG selection list. Without a max_length an
    # attacker can pad a 64KB body with ~3500 FpgInterestIn entries, each
    # of which the A1 fabrication path allocates a UUID for. The Form B UI
    # exposes a handful of selections at a time; 20 is a generous ceiling
    # that no legitimate submission will hit (review with product if it ever
    # does — likely a different schema is appropriate at that scale).
    fpg_selections: list[FpgInterestIn] = Field(default_factory=list, max_length=20)


class FacilitationIntake(IntakeBase):
    """Form A (`/facilitate-adoption`) payload: a facilitator (org or person)."""

    party_kind: Literal["facilitator"] = "facilitator"
    organization_name: str | None = Field(default=None, max_length=512)


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
