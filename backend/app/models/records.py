from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict


ApprovalStatus = Literal["pending", "approved", "rejected"]


class DatabaseStatus(BaseModel):
    status: Literal["connected"]


class Commitment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    description: str
    owner_name: str | None = None
    due_at: datetime | None = None
    approval_status: ApprovalStatus
    created_at: datetime


class Decision(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    summary: str
    approval_status: ApprovalStatus
    created_at: datetime


class StoredMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    tenant_id: UUID
    room_id: UUID
    switch_message_id: str
    author_name: str | None = None
    body: str
    occurred_at: datetime
    extraction_status: str
    created_at: datetime
