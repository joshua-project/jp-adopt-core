from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (
        CheckConstraint(
            "adopter_status IS NULL OR adopter_status IN ("
            "'draft', 'new', 'potential_adopter', 'contacted', 'engaged', "
            "'matched', 'sent_back', 'active', 'inactive', 'do_not_engage')",
            name="ck_contacts_adopter_status",
        ),
        CheckConstraint(
            "facilitator_status IS NULL OR facilitator_status IN ("
            "'draft', 'new', 'not_ready', 'ready', 'do_not_engage')",
            name="ck_contacts_facilitator_status",
        ),
        Index("ix_contacts_b2c_subject_id", "b2c_subject_id"),
        Index(
            "uq_contacts_email_normalized",
            "email_normalized",
            unique=True,
            postgresql_where="email_normalized IS NOT NULL",
        ),
        Index("ix_contacts_source_system_source_id", "source_system", "source_id"),
        # Partial unique index — added in migration 0009 to give the DT ETL
        # an ON CONFLICT target for its idempotent upsert. Local rows (no
        # source) are excluded so we don't constrain them.
        Index(
            "uq_contacts_source_system_source_id",
            "source_system",
            "source_id",
            unique=True,
            postgresql_where=(
                "source_system IS NOT NULL AND source_id IS NOT NULL"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    party_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(512), nullable=False)
    adopter_status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    facilitator_status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1", default=1
    )
    b2c_subject_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_normalized: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_system: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_modified_after_import: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    origin: Mapped[str | None] = mapped_column(Text, nullable=True)
    newsletter_opt_in: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    country_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    language_codes: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ContactProfile(Base):
    """U6: 1:1 adoption-field profile for a Contact. Holds the JP-custom
    ``dt-adoption-fields`` plugin fields (docs/dt-parity-inventory.md §2.6 A).
    Separate from ``contacts`` so profile edits don't churn ``Contact.version``
    (the optimistic-lock column the match/transition flows gate on). All
    columns nullable; enum CHECKs mirror migration 0012 + the plugin option sets.
    """

    __tablename__ = "contact_profile"
    __table_args__ = (
        UniqueConstraint("contact_id", name="uq_contact_profile_contact_id"),
        CheckConstraint(
            "entity_size IS NULL OR entity_size IN "
            "('1', 'lt_30', '31_100', '101_500', '501_2000', '2001_plus')",
            name="ck_contact_profile_entity_size",
        ),
        CheckConstraint(
            "preferred_communication IS NULL OR preferred_communication IN "
            "('email', 'phone')",
            name="ck_contact_profile_preferred_communication",
        ),
        CheckConstraint(
            "adopter_type IS NULL OR adopter_type IN "
            "('individual', 'small_group', 'church', 'organization', 'network')",
            name="ck_contact_profile_adopter_type",
        ),
        CheckConstraint(
            "mou_status IS NULL OR mou_status IN "
            "('signed', 'not_required', 'not_sent')",
            name="ck_contact_profile_mou_status",
        ),
        CheckConstraint(
            "engagement_score IS NULL OR "
            "(engagement_score >= 0 AND engagement_score <= 100)",
            name="ck_contact_profile_engagement_score_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    # contact_info tile
    ministry_areas: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    entity_size: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_contact_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    secondary_contact_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    secondary_contact_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    secondary_contact_phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    website: Mapped[str | None] = mapped_column(Text, nullable=True)
    preferred_communication: Mapped[str | None] = mapped_column(Text, nullable=True)
    form_country: Mapped[str | None] = mapped_column(Text, nullable=True)
    form_state_region: Mapped[str | None] = mapped_column(Text, nullable=True)
    # adoption_profile tile
    adopter_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    commitment_types: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    commitment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # facilitation_profile tile
    works_with_fpgs: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    willing_to_facilitate: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    facilitation_entity_types: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), nullable=True
    )
    facilitation_entity_sizes: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), nullable=True
    )
    mou_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    mou_signature_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    # connection_prefs tile
    want_facilitator_connection: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )
    facilitator_entity_types: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), nullable=True
    )
    desired_facilitator_info: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), nullable=True
    )
    # network_prefs tile
    want_network_connection: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    network_partner_info: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), nullable=True
    )
    # vetting tile
    has_doctrinal_distinctives: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )
    doctrinal_distinctives: Mapped[str | None] = mapped_column(Text, nullable=True)
    has_accountability_membership: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )
    accountability_memberships: Mapped[str | None] = mapped_column(Text, nullable=True)
    # engagement tile
    last_contact_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    engagement_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_followup_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # form_submission tile
    referral_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    campaign: Mapped[str | None] = mapped_column(Text, nullable=True)
    partner: Mapped[str | None] = mapped_column(Text, nullable=True)
    additional_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_download_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Outbox(Base):
    __tablename__ = "outbox"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_type: Mapped[str] = mapped_column(String(256), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # U10: separate drain marks this when the drip engine has consumed
    # the event (enrolled contacts, exited do_not_engage cohorts, etc.).
    drip_processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (
        PrimaryKeyConstraint("user_b2c_subject_id", "role_id"),
    )

    user_b2c_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id"), nullable=False
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TransitionAudit(Base):
    __tablename__ = "transition_audit"
    __table_args__ = (Index("ix_transition_audit_contact_id", "contact_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False
    )
    from_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_state: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_role: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    outbox_event_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IdentityLink(Base):
    __tablename__ = "identity_link"
    __table_args__ = (
        Index("ix_identity_link_email_normalized", "email_normalized"),
        Index(
            "uq_identity_link_b2c_subject_id",
            "b2c_subject_id",
            unique=True,
            postgresql_where="b2c_subject_id IS NOT NULL",
        ),
        # Fail-closed guard on the magic-link first-claim race: at most one
        # ``magic_link`` IdentityLink per email_normalized. The CAS update on
        # ``magic_link_token.claimed_at`` blocks token reuse; this index
        # blocks the rare pre-CAS duplicate IdentityLink insert.
        Index(
            "uq_identity_link_magic_email",
            "email_normalized",
            unique=True,
            postgresql_where="idp_name = 'magic_link'",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    b2c_subject_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    email_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    idp_name: Mapped[str] = mapped_column(Text, nullable=False)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MagicLinkToken(Base):
    __tablename__ = "magic_link_token"
    __table_args__ = (
        Index("ix_magic_link_token_email_normalized", "email_normalized"),
        Index("ix_magic_link_token_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    email_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    requested_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claimed_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)


class MagicLinkRateLimit(Base):
    __tablename__ = "magic_link_rate_limit"
    __table_args__ = (
        Index(
            "ix_magic_link_rate_limit_email_requested",
            "email_normalized",
            "requested_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PartnerTenant(Base):
    __tablename__ = "partner_tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    microsoft_tenant_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    partner_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    partner_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MigrationConflict(Base):
    __tablename__ = "migration_conflicts"
    __table_args__ = (
        Index(
            "ix_migration_conflicts_source_table",
            "source_system",
            "source_id",
            "table_name",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    conflict_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    local_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FacilitatingOrg(Base):
    __tablename__ = "facilitating_org"
    __table_args__ = (
        CheckConstraint(
            "capacity_committed >= 0",
            name="ck_facilitating_org_capacity_committed_nonneg",
        ),
        CheckConstraint(
            "capacity_committed <= capacity_total",
            name="ck_facilitating_org_capacity_committed_le_total",
        ),
        Index(
            "ix_facilitating_org_active_accepting",
            "active",
            "accepting_potential_adopters",
        ),
        Index(
            "ix_facilitating_org_source_system_source_id",
            "source_system",
            "source_id",
        ),
        Index(
            "uq_facilitating_org_is_triage_org",
            "is_triage_org",
            unique=True,
            postgresql_where="is_triage_org = TRUE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    country_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    language_codes: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), nullable=True
    )
    theological_tags: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), nullable=True
    )
    capacity_total: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    capacity_committed: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    # N6: currently informational only. F36 added a ``contact_has_no_rop3``
    # parameter to ``hard_filter`` that consulted this column, but the
    # only production caller short-circuits no-rop3 interests to triage
    # before ``hard_filter`` runs, so the wiring was reverted as dead
    # code. The column stays because the planned U7+ triage-reassignment
    # path will read it: when an adopter arrives without an FPG selection,
    # ``match_or_route`` will pick from facilitators with
    # ``accepting_potential_adopters=True AND is_triage_org=False`` as
    # alternative triage targets. Until then, operators can populate the
    # flag in advance; nothing in v1 depends on it.
    accepting_potential_adopters: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    is_triage_org: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    last_assigned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_system: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Fpg(Base):
    __tablename__ = "fpg"
    __table_args__ = (
        Index("ix_fpg_country_code", "country_code"),
        Index(
            "ix_fpg_frontier",
            "frontier",
            postgresql_where="frontier = TRUE",
        ),
    )

    rop3: Mapped[str] = mapped_column(Text, primary_key=True)
    people_id3: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    country_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    language_codes: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), nullable=True
    )
    frontier: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FacilitatorFpgCoverage(Base):
    __tablename__ = "facilitator_fpg_coverage"
    __table_args__ = (
        PrimaryKeyConstraint(
            "facilitator_org_id", "rop3", name="pk_facilitator_fpg_coverage"
        ),
        Index("ix_facilitator_fpg_coverage_rop3", "rop3"),
    )

    facilitator_org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("facilitating_org.id", ondelete="CASCADE"),
        nullable=False,
    )
    rop3: Mapped[str] = mapped_column(
        Text,
        ForeignKey("fpg.rop3", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AdopterInterest(Base):
    __tablename__ = "adopter_interest"
    __table_args__ = (
        Index("ix_adopter_interest_contact_id", "contact_id"),
        Index("ix_adopter_interest_rop3", "rop3"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    rop3: Mapped[str | None] = mapped_column(
        Text, ForeignKey("fpg.rop3"), nullable=True
    )
    commitment_level: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Match(Base):
    __tablename__ = "match"
    __table_args__ = (
        CheckConstraint(
            "status IN ("
            "'recommended', 'accepted', 'sent_back', 'declined', "
            "'active', 'completed', 'withdrawn', 'triage')",
            name="ck_match_status",
        ),
        Index("ix_match_adopter_interest_id", "adopter_interest_id"),
        Index("ix_match_facilitator_org_id", "facilitator_org_id"),
        Index("ix_match_status", "status"),
        Index(
            "uq_match_open_per_interest",
            "adopter_interest_id",
            unique=True,
            postgresql_where=(
                "status IN ('recommended', 'accepted', 'active', 'triage')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    adopter_interest_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("adopter_interest.id", ondelete="CASCADE"),
        nullable=False,
    )
    facilitator_org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("facilitating_org.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decided_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_reason_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ApiIdempotencyKey(Base):
    """Request-deduplication for intake endpoints (U4).

    A pending row is inserted on first sight; once the handler completes, the
    row flips to `state='completed'` with the cached response body. Replays
    within the dedup window return the cached body verbatim.
    """

    __tablename__ = "api_idempotency_keys"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'completed')",
            name="ck_api_idempotency_keys_state",
        ),
        # F42: mirror the DDL constraint created by migration 0004 so the
        # ORM-level declarative model carries the same uniqueness contract.
        # Without it, ``Base.metadata`` does not know the column pair is
        # unique and tooling that introspects the model would propose
        # adding a duplicate constraint.
        UniqueConstraint(
            "api_key_id", "key", name="uq_api_idempotency_keys_apikey_key"
        ),
        Index("ix_api_idempotency_keys_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    api_key_id: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        # DM-007: match the migration form (``sa.text("'pending'")``). A bare
        # ``"pending"`` server_default sends ``DEFAULT 'pending'`` correctly
        # at DDL time but autogenerate diffs it as different from the migration
        # version and produces a spurious ALTER COLUMN proposal.
        server_default=text("'pending'"),
        default="pending",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now() + interval '24 hours'"),
    )


class SubmissionBlocked(Base):
    """Anti-enumeration log: submissions matching a `do_not_engage` contact are
    silently dropped to the caller (201 Created — matching the accepted-first-
    call status so the response is indistinguishable from a real submission)
    but persisted here so Amy can audit blocked attempts and reverse course
    if needed."""

    __tablename__ = "submissions_blocked"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
    )
    email_normalized: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    submission_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    blocked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StaffIdentityLink(Base):
    """U9: maps a DT wp_users row to a B2C subject ID (when one exists) and
    captures email + display name. The activity_log table's ``author_id``
    column resolves through this — DT comments authored by a wp_users row
    that was later deleted are routed to a synthetic
    ``system:dt_legacy_unknown`` author and never reach this table.
    """

    __tablename__ = "staff_identity_link"
    __table_args__ = (
        Index(
            "uq_staff_identity_link_dt_user_id",
            "dt_user_id",
            unique=True,
        ),
        Index(
            "uq_staff_identity_link_b2c_subject_id",
            "b2c_subject_id",
            unique=True,
            postgresql_where="b2c_subject_id IS NOT NULL",
        ),
        Index(
            "ix_staff_identity_link_email_normalized",
            "email_normalized",
        ),
        CheckConstraint(
            "status IN ('active', 'inactive', 'unknown')",
            name="ck_staff_identity_link_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    dt_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    b2c_subject_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    email_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'"), default="active"
    )
    source_system: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'dt'"), default="dt"
    )
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ActivityLog(Base):
    """U9: DT wp_comments + wp_dt_activity_log rows mapped per contact.
    Preserves authorship via ``author_id`` (string — either a
    ``staff_identity_link.id`` UUID for resolved authors, or the synthetic
    ``system:dt_legacy_unknown`` sentinel for deleted authors). Threading
    is preserved via ``parent_id`` self-FK.
    """

    __tablename__ = "activity_log"
    __table_args__ = (
        Index("ix_activity_log_contact_id", "contact_id"),
        Index(
            "ix_activity_log_parent_id",
            "parent_id",
            postgresql_where="parent_id IS NOT NULL",
        ),
        Index(
            "uq_activity_log_source_system_source_id",
            "source_system",
            "source_id",
            unique=True,
            postgresql_where="source_id IS NOT NULL",
        ),
        Index("ix_activity_log_occurred_at", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    author_id: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("activity_log.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_system: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'local'"), default="local"
    )
    source_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EtlRun(Base):
    """U9: one row per ETL invocation. ``source_max_modified_at`` is the
    watermark the next incremental run uses as its ``> ?`` floor.
    """

    __tablename__ = "etl_run"
    __table_args__ = (
        Index("ix_etl_run_table_started", "table_name", "started_at"),
        CheckConstraint(
            "mode IN ('dry_run', 'production')",
            name="ck_etl_run_mode",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'production'"),
        default="production",
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_max_modified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    watermark_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rows_in: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    rows_out_inserted: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    rows_out_updated: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    rows_out_skipped: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    rows_in_conflict: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    errors: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class EtlDeletedInSource(Base):
    """U9: an ETL run that watches for vanished rows in source MySQL records
    them here. ETL never hard-deletes the corresponding Postgres row — Amy
    reviews this table and decides per case.
    """

    __tablename__ = "etl_deleted_in_source"
    __table_args__ = (
        Index("ix_etl_deleted_in_source_run", "etl_run_id"),
        Index(
            "ix_etl_deleted_in_source_source",
            "source_system",
            "source_id",
            "table_name",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    etl_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("etl_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FacilitatorOrgMembership(Base):
    """U8: M:N link between a B2C-authenticated user and the facilitator orgs
    they belong to. The facilitator portal filters Match rows by
    ``facilitator_org_id IN <memberships of this actor>``.
    """

    __tablename__ = "facilitator_org_membership"
    __table_args__ = (
        PrimaryKeyConstraint(
            "user_b2c_subject_id",
            "facilitator_org_id",
            name="pk_facilitator_org_membership",
        ),
        # F33: keep the ORM-side CHECK in lockstep with migration 0008.
        CheckConstraint(
            "role_in_org IN ('member', 'admin')",
            name="ck_facilitator_org_membership_role_in_org",
        ),
        Index(
            "ix_facilitator_org_membership_facilitator_org_id",
            "facilitator_org_id",
        ),
    )

    user_b2c_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    facilitator_org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("facilitating_org.id", ondelete="CASCADE"),
        nullable=False,
    )
    role_in_org: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'member'"), default="member"
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FacilitatorOutboxSubscription(Base):
    """U8: per-org HMAC webhook destination for outbox events. Empty in week 1;
    populated as facilitator orgs come online with their own back-ends.
    """

    __tablename__ = "facilitator_outbox_subscriptions"
    __table_args__ = (
        Index(
            "ix_facilitator_outbox_subscriptions_org",
            "facilitator_org_id",
        ),
        Index(
            "ix_facilitator_outbox_subscriptions_active",
            "active",
            postgresql_where="active = TRUE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    facilitator_org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("facilitating_org.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type_glob: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'jp.adopt.v1.match.*'"),
        default="jp.adopt.v1.match.*",
    )
    endpoint_url: Mapped[str] = mapped_column(Text, nullable=False)
    hmac_key: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class MatchAttempt(Base):
    __tablename__ = "match_attempt"
    __table_args__ = (
        Index("ix_match_attempt_contact_id", "contact_id"),
        Index("ix_match_attempt_run_id", "run_id"),
        Index("ix_match_attempt_contact_run", "contact_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    adopter_interest_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("adopter_interest.id"),
        nullable=True,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    candidate_facilitator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("facilitating_org.id"),
        nullable=False,
    )
    score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    score_breakdown: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    filter_results: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Campaign(Base):
    """U10: top-level marketing campaign. ``status`` gates the worker
    drain: only active campaigns trigger enrollment from outbox events.
    Editing step content/timing bumps ``version`` so in-flight
    enrollments stay pinned to the version they started under via
    ``Enrollment.campaign_version``.
    """

    __tablename__ = "campaign"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'active', 'paused', 'archived')",
            name="ck_campaign_status",
        ),
        CheckConstraint(
            "trigger_type IN ('event', 'manual')",
            name="ck_campaign_trigger_type",
        ),
        Index(
            "ix_campaign_status",
            "status",
            postgresql_where="status = 'active'",
        ),
        Index(
            "ix_campaign_trigger_event_type",
            "trigger_event_type",
            postgresql_where=(
                "trigger_event_type IS NOT NULL AND status = 'active'"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'draft'"), default="draft"
    )
    trigger_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'event'"), default="event"
    )
    trigger_event_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_enroll_existing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    precedence: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1"), default=1
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CampaignStep(Base):
    """U10: one ordered step in a campaign. ``mjml_template_name`` is a
    filename in ``apps/api/email-templates/``, not inline content."""

    __tablename__ = "campaign_step"
    __table_args__ = (
        Index(
            "uq_campaign_step_campaign_position",
            "campaign_id",
            "position",
            unique=True,
        ),
        CheckConstraint(
            "position >= 0", name="ck_campaign_step_position_nonneg"
        ),
        CheckConstraint(
            "delay_days >= 0", name="ck_campaign_step_delay_days_nonneg"
        ),
        CheckConstraint(
            "send_at_hour >= 0 AND send_at_hour <= 23",
            name="ck_campaign_step_send_at_hour_range",
        ),
        CheckConstraint(
            "send_at_minute >= 0 AND send_at_minute <= 59",
            name="ck_campaign_step_send_at_minute_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaign.id", ondelete="CASCADE"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    delay_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    mjml_template_name: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    send_at_hour: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("9"), default=9
    )
    send_at_minute: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Enrollment(Base):
    """U10: per-(campaign, contact) state row. Partial unique index on
    (campaign_id, contact_id) WHERE state IN (pending, active, paused)
    enforces one open enrollment per contact per campaign while still
    permitting historical completed/exited rows to coexist for audit.
    """

    __tablename__ = "enrollment"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'active', 'paused', 'completed', 'exited')",
            name="ck_enrollment_state",
        ),
        CheckConstraint(
            "current_step_position >= -1",
            name="ck_enrollment_step_position_nonneg",
        ),
        Index(
            "uq_enrollment_open_per_campaign_contact",
            "campaign_id",
            "contact_id",
            unique=True,
            postgresql_where="state IN ('pending', 'active', 'paused')",
        ),
        Index("ix_enrollment_contact_id", "contact_id"),
        Index(
            "ix_enrollment_state_step",
            "state",
            "current_step_position",
            postgresql_where="state = 'active'",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaign.id", ondelete="RESTRICT"),
        nullable=False,
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    campaign_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1"), default=1
    )
    current_step_position: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'pending'"),
        default="pending",
    )
    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_step_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    exited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    exit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class EnrollmentEvent(Base):
    """U10: append-only log of enrollment-state events. ``payload``
    carries the per-event metadata (step_position for step_sent, error
    text for send_failed, etc.). BIGSERIAL id because the table will
    grow at multiple-events-per-enrollment cadence and we want gap-free
    ordering for replay.
    """

    __tablename__ = "enrollment_event"
    __table_args__ = (
        Index(
            "ix_enrollment_event_enrollment_id",
            "enrollment_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=False), primary_key=True
    )
    enrollment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("enrollment.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SuppressionList(Base):
    """U10: emails the engine must never send to. Keyed by SHA-256 hex of
    the normalized email so no raw PII lives in the table. Hard filter
    at send time."""

    __tablename__ = "suppression_list"
    __table_args__ = (
        PrimaryKeyConstraint("email_hash", name="pk_suppression_list"),
        Index("ix_suppression_list_suppressed_at", "suppressed_at"),
    )

    email_hash: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    suppressed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    source_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )


class DigestRun(Base):
    """U11: one row per daily-digest cron invocation."""

    __tablename__ = "digest_run"
    __table_args__ = (
        Index("ix_digest_run_window_start", "window_start"),
        CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'empty')",
            name="ck_digest_run_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'pending'"),
        default="pending",
    )
    recipient_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    match_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class DigestRecipient(Base):
    """U11: one row per (digest_run, recipient_address)."""

    __tablename__ = "digest_recipient"
    __table_args__ = (
        Index("ix_digest_recipient_run", "digest_run_id"),
        Index(
            "uq_digest_recipient_run_address",
            "digest_run_id",
            "recipient_address",
            unique=True,
        ),
        CheckConstraint(
            "recipient_kind IN ('all_staff', 'adoption_manager', 'facilitator')",
            name="ck_digest_recipient_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'skipped')",
            name="ck_digest_recipient_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    digest_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("digest_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    recipient_address: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_kind: Mapped[str] = mapped_column(Text, nullable=False)
    facilitator_org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("facilitating_org.id", ondelete="SET NULL"),
        nullable=True,
    )
    match_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    match_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'pending'"),
        default="pending",
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
