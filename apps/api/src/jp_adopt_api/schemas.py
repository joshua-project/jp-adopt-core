from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ContactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    party_kind: str
    display_name: str
    adopter_status: str | None
    facilitator_status: str | None
    created_at: datetime
    updated_at: datetime


class ContactListResponse(BaseModel):
    items: list[ContactRead]
    total: int
    limit: int
    offset: int


class ContactPatch(BaseModel):
    party_kind: str | None = Field(default=None, min_length=1, max_length=64)
    display_name: str | None = Field(default=None, min_length=1, max_length=512)
    adopter_status: str | None = Field(default=None, max_length=128)
    facilitator_status: str | None = Field(default=None, max_length=128)
