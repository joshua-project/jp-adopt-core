from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    func,
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
