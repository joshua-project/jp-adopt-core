from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
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
