"""Source-backed, room-scoped person profile contracts."""

from typing import Literal
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


class ExtractedProfileFact(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)

    attribute: Literal["responsibility", "skill", "preference", "timezone"]
    value: str = Field(min_length=1, max_length=256, repr=False)
    evidence: str = Field(min_length=1, max_length=1000, repr=False)
    correction: bool = False


class ProfileRecord(BaseModel):
    model_config = ConfigDict(extra='ignore', hide_input_in_errors=True)
    id: UUID
    tenant_id: UUID
    room_id: UUID
    switch_author_id: str = Field(min_length=1, max_length=512, repr=False)
    display_name: str = Field(repr=False)
    created_at: datetime


class ProfileClaim(BaseModel):
    model_config = ConfigDict(extra='ignore', hide_input_in_errors=True)
    id: UUID
    tenant_id: UUID
    room_id: UUID
    profile_id: UUID
    attribute: Literal['responsibility', 'skill', 'preference', 'timezone']
    value: str = Field(repr=False)
    status: Literal['active', 'superseded']
    created_at: datetime


class ProfileEvidence(BaseModel):
    model_config = ConfigDict(extra='ignore', hide_input_in_errors=True)
    tenant_id: UUID
    room_id: UUID
    claim_id: UUID
    message_id: UUID
    memory_id: UUID
    quote: str = Field(repr=False)
    correction: bool
    occurred_at: datetime


class ProfilePage(BaseModel):
    items: list[ProfileRecord]
    next_cursor: UUID | None


class ProfileDetail(BaseModel):
    profile: ProfileRecord
    claims: list[ProfileClaim]
    evidence: list[ProfileEvidence]
    truncated: bool
    conflicting_attributes: list[str] = Field(default_factory=list)
    verification: Literal['reported_statements'] = 'reported_statements'
