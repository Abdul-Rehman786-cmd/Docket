from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict


ApprovalStatus = Literal["pending", "approved", "rejected"]


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
