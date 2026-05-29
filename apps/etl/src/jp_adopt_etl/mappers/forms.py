"""Map jp-adopt-forms DB rows → adopt-core intake payloads.

Field mapping mirrors ``jp-adopt-forms/src/lib/core-client.ts`` (the live
dual-write path). The forms schema uses ``adoption_submissions`` and
``facilitation_submissions`` — not a single JSONB ``submissions`` table.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from jp_adopt_api.schemas import AdoptionIntake, FacilitationIntake
from pydantic import ValidationError

FormType = Literal["adoption", "facilitation"]


@dataclass(frozen=True)
class MapSuccess:
    form_type: FormType
    payload: AdoptionIntake | FacilitationIntake
    source_id: str
    created_at: datetime


@dataclass(frozen=True)
class MapFailure:
    reason: str
    source_id: str
    source_payload: dict[str, Any]


MapResult = MapSuccess | MapFailure


def _compact(obj: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in obj.items():
        if value is None:
            continue
        if isinstance(value, list) and len(value) == 0:
            continue
        out[key] = value
    return out


def _to_core_consents(raw: Any) -> list[dict[str, Any]] | None:
    if not raw or not isinstance(raw, list):
        return None
    consents: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        consents.append(
            {
                "consent_type": item.get("consentType") or item.get("consent_type"),
                "version": item.get("version"),
                "content_hash": item.get("contentHash") or item.get("content_hash"),
                "accepted_at": item.get("acceptedAt") or item.get("accepted_at"),
                "conversation_id": item.get("conversationId")
                or item.get("conversation_id"),
                "evidence": item.get("evidence"),
            }
        )
    return consents or None


def _map_adoption(
    submission: dict[str, Any],
    fpg_selections: list[dict[str, Any]],
    *,
    source_id: str,
    created_at: datetime,
) -> MapResult:
    email = submission.get("email")
    if not email:
        return MapFailure(
            reason="validation_error: missing email",
            source_id=source_id,
            source_payload=submission,
        )

    all_commitments = sorted(
        {
            ct
            for sel in fpg_selections
            for ct in (sel.get("commitment_types") or [])
        }
    )
    profile = _compact(
        {
            "adopter_type": submission.get("adopter_type"),
            "entity_size": submission.get("entity_size"),
            "form_country": submission.get("country"),
            "form_state_region": submission.get("state_region"),
            "preferred_communication": submission.get("preferred_communication"),
            "primary_contact_name": submission.get("contact_name"),
            "secondary_contact_name": submission.get("secondary_contact_name"),
            "secondary_contact_email": submission.get("secondary_contact_email"),
            "secondary_contact_phone": submission.get("secondary_contact_phone"),
            "website": submission.get("website"),
            "mou_status": "signed" if submission.get("mou_accepted") else "not_sent",
            "has_doctrinal_distinctives": submission.get("has_doctrinal_distinctives")
            or False,
            "doctrinal_distinctives": submission.get("doctrinal_distinctives"),
            "ministry_areas": submission.get("ministry_areas"),
            "commitment_types": all_commitments,
            "want_facilitator_connection": (
                submission.get("want_facilitator_connection") or False
            )
            or (submission.get("want_partner_connection") or False),
            "facilitator_entity_types": submission.get("partner_entity_types"),
            "desired_facilitator_info": submission.get("desired_partner_info"),
            "additional_notes": submission.get("additional_notes"),
            "referral_source": submission.get("referral_source"),
            "campaign": submission.get("campaign"),
            "partner": submission.get("partner"),
        }
    )

    body: dict[str, Any] = {
        "email": email,
        "display_name": submission.get("entity_name") or submission.get("entityName"),
        "origin": "website",
        "newsletter_opt_in": submission.get("newsletter_opt_in")
        or submission.get("newsletterOptIn")
        or False,
        "profile": profile,
        "fpg_selections": [
            {
                "people_id3": sel.get("people_id3"),
                "commitment_types": sel.get("commitment_types") or [],
            }
            for sel in fpg_selections
        ],
    }
    consents = _to_core_consents(submission.get("consents_accepted"))
    if consents:
        body["consents"] = consents
    if submission.get("geo_country_code"):
        body["country_code"] = str(submission["geo_country_code"]).upper()

    try:
        payload = AdoptionIntake.model_validate(body)
    except ValidationError as exc:
        return MapFailure(
            reason=f"validation_error: {exc.errors()[0]['msg']}",
            source_id=source_id,
            source_payload=submission,
        )
    return MapSuccess(
        form_type="adoption",
        payload=payload,
        source_id=source_id,
        created_at=created_at,
    )


def _map_facilitation(
    submission: dict[str, Any],
    fpg_selections: list[dict[str, Any]],
    *,
    source_id: str,
    created_at: datetime,
) -> MapResult:
    email = submission.get("primary_contact_email") or submission.get(
        "primaryContactEmail"
    )
    if not email:
        return MapFailure(
            reason="validation_error: missing email",
            source_id=source_id,
            source_payload=submission,
        )

    entity_types: list[str] = []
    if submission.get("partner_with_individuals"):
        entity_types.append("individuals")
    if submission.get("partner_with_small_groups"):
        entity_types.append("small_groups")
    if submission.get("partner_with_churches"):
        entity_types.append("churches")
    if submission.get("partner_with_orgs"):
        entity_types.append("organizations")
    if submission.get("partner_with_networks"):
        entity_types.append("networks")

    all_network = sorted(
        {
            svc
            for sel in fpg_selections
            for svc in (sel.get("network_services") or [])
        }
    )

    profile = _compact(
        {
            "entity_size": submission.get("entity_size"),
            "form_country": submission.get("country"),
            "form_state_region": submission.get("state_region"),
            "preferred_communication": submission.get("preferred_communication"),
            "primary_contact_name": submission.get("primary_contact_name"),
            "secondary_contact_name": submission.get("secondary_contact_name"),
            "secondary_contact_email": submission.get("secondary_contact_email"),
            "secondary_contact_phone": submission.get("secondary_contact_phone"),
            "website": submission.get("website"),
            "works_with_fpgs": submission.get("works_with_fpgs"),
            "willing_to_facilitate": submission.get("willing_to_facilitate") or False,
            "want_network_connection": submission.get("want_network_connection")
            or False,
            "mou_status": submission.get("mou_status"),
            "mou_signature_name": submission.get("mou_signature_name"),
            "facilitation_entity_types": entity_types,
            "ministry_areas": submission.get("ministry_areas"),
            "has_doctrinal_distinctives": submission.get("has_doctrinal_distinctives")
            or False,
            "doctrinal_distinctives": submission.get("doctrinal_distinctives"),
            "has_accountability_membership": submission.get(
                "has_accountability_membership"
            )
            or False,
            "accountability_memberships": submission.get("accountability_memberships"),
            "network_partner_info": all_network,
            "referral_source": submission.get("referral_source"),
            "campaign": submission.get("campaign"),
            "partner": submission.get("partner"),
        }
    )

    org_name = submission.get("org_name") or submission.get("orgName")
    body: dict[str, Any] = {
        "email": email,
        "display_name": org_name,
        "origin": "website",
        "newsletter_opt_in": submission.get("newsletter_opt_in")
        or submission.get("newsletterOptIn")
        or False,
        "organization_name": org_name,
        "profile": profile,
        "fpg_selections": [
            {
                "people_id3": sel.get("people_id3"),
                "engagement_status": sel.get("engagement_status"),
                "facilitation_services": sel.get("facilitation_services") or [],
                "network_services": sel.get("network_services") or [],
            }
            for sel in fpg_selections
        ],
    }
    if submission.get("geo_country_code"):
        body["country_code"] = str(submission["geo_country_code"]).upper()

    try:
        payload = FacilitationIntake.model_validate(body)
    except ValidationError as exc:
        return MapFailure(
            reason=f"validation_error: {exc.errors()[0]['msg']}",
            source_id=source_id,
            source_payload=submission,
        )
    return MapSuccess(
        form_type="facilitation",
        payload=payload,
        source_id=source_id,
        created_at=created_at,
    )


def map_submission_row(row: dict[str, Any]) -> MapResult:
    """Translate one :func:`forms_source.iter_submissions` row."""
    form_type = row.get("form_type")
    source_id = str(row.get("id") or "")
    created_at = row.get("created_at")
    if not isinstance(created_at, datetime):
        return MapFailure(
            reason="missing_created_at",
            source_id=source_id,
            source_payload=dict(row),
        )
    submission = row.get("submission") or {}
    if not isinstance(submission, dict):
        submission = dict(submission) if submission else {}
    fpg_selections = row.get("fpg_selections") or []
    if not isinstance(fpg_selections, list):
        fpg_selections = []

    if form_type == "adoption":
        return _map_adoption(
            submission,
            fpg_selections,
            source_id=source_id,
            created_at=created_at,
        )
    if form_type == "facilitation":
        return _map_facilitation(
            submission,
            fpg_selections,
            source_id=source_id,
            created_at=created_at,
        )
    return MapFailure(
        reason="unknown_form_type",
        source_id=source_id,
        source_payload=dict(row),
    )


__all__ = [
    "MapFailure",
    "MapResult",
    "MapSuccess",
    "map_submission_row",
]
