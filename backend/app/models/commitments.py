"""Commitment lifecycle contracts. Shared tokens are not human identities."""

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

SubjectId = Annotated[str, StringConstraints(min_length=1, max_length=512, pattern=r"^\S+$")]
Role = Literal["member", "approver", "admin"]
ApprovalStatus = Literal["pending", "approved", "rejected"]
TaskStatus = Literal["not_started", "in_progress", "completed", "cancelled"]


class Action(str, Enum):
    approve = "approve"
    reject = "reject"
    start = "start"
    complete = "complete"
    cancel = "cancel"
    reassign = "reassign"
    reschedule = "reschedule"


class Scope(BaseModel):
    model_config = ConfigDict(frozen=True)
    tenant_id: UUID
    room_id: UUID  # Internal room UUID, not an inferred Switch room ID.


class Actor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    subject_id: SubjectId = Field(repr=False)


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    idempotency_key: UUID
    expected_version: Annotated[int, Field(strict=True, ge=1, le=9223372036854775807)]
    reason: str | None = Field(default=None, max_length=1000, repr=False)

    @field_validator("reason")
    @classmethod
    def trim_reason(cls, value):
        return (value.strip() or None) if value is not None else None

    def payload(self) -> dict:
        return self.model_dump(mode="json", exclude={"idempotency_key", "expected_version"})


class ApproveRequest(ActionRequest):
    pass


class RejectRequest(ActionRequest):
    pass


class StartRequest(ActionRequest):
    pass


class CompleteRequest(ActionRequest):
    pass


class CancelRequest(ActionRequest):
    pass


class ReassignRequest(ActionRequest):
    assignee_id: SubjectId


class RescheduleRequest(ActionRequest):
    due_at: AwareDatetime | None  # Required: null explicitly clears; omission is invalid.

    @field_validator("due_at", mode="before")
    @classmethod
    def timestamp_only(cls, value):
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError("Use a timezone-aware timestamp or null")
        return value

    @field_validator("due_at")
    @classmethod
    def utc_deadline(cls, value):
        return value.astimezone(timezone.utc) if value is not None else None


class CommitmentState(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, hide_input_in_errors=True)
    id: UUID
    tenant_id: UUID
    room_id: UUID
    message_id: UUID
    extraction_key: str
    description: str = Field(repr=False)
    owner_name: str | None = Field(default=None, repr=False)
    owner_switch_user_id: SubjectId | None = None
    due_at: AwareDatetime | None = None
    approval_status: ApprovalStatus = "pending"
    approval_by_switch_user_id: SubjectId | None = None
    approval_at: AwareDatetime | None = None
    task_status: TaskStatus = "not_started"
    completed_at: AwareDatetime | None = None
    cancelled_at: AwareDatetime | None = None
    automated_acceptance_at: AwareDatetime | None = None
    automated_acceptance_source_id: UUID | None = None
    next_reminder_at: AwareDatetime | None = None
    last_chased_at: AwareDatetime | None = None
    version: int = Field(default=1, ge=1)
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def valid_state(self):
        if (self.task_status == "completed") != (self.completed_at is not None):
            raise ValueError("Completion status and timestamp disagree")
        if self.approval_status != "pending" and (not self.approval_by_switch_user_id or self.approval_at is None):
            raise ValueError("Reviewed commitments require reviewer and timestamp")
        if self.automated_acceptance_at is not None or self.automated_acceptance_source_id is not None:
            if (self.automated_acceptance_at is None or self.automated_acceptance_source_id is None
                or self.approval_status!='pending' or self.task_status!='completed'):
                raise ValueError('Automated owner acceptance requires a completed pending task and source')
        return self


class ReminderState(BaseModel):
    eligible: bool
    reason: Literal["eligible", "pending", "rejected", "completed", "cancelled", "unassigned", "no_deadline"]
    due_at: AwareDatetime | None
    assignee_id: SubjectId | None


class CommitmentView(BaseModel):
    commitment: CommitmentState
    reminder: ReminderState


class AuditEvent(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    id: UUID
    sequence: int
    tenant_id: UUID
    room_id: UUID
    commitment_id: UUID
    actor_id: SubjectId
    action: Action
    occurred_at: AwareDatetime
    idempotency_key: UUID
    expected_version: int
    changed: bool
    before: dict = Field(repr=False)
    after: dict = Field(repr=False)
    reason: str | None = Field(default=None, repr=False)
    backend_actor: str | None = None
    source_evidence_id: UUID | None = None
    evidence: str | None = Field(default=None, repr=False)
    outcome: Literal['applied','no_change'] = 'applied'


class ActionResult(CommitmentView):
    event: AuditEvent
    changed: bool


class Filters(BaseModel):
    approval_status: ApprovalStatus | None = None
    task_status: TaskStatus | None = None
    assignee_id: SubjectId | None = None
    due_from: AwareDatetime | None = None
    due_to: AwareDatetime | None = None
    limit: int = Field(default=25, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=10000)

    @model_validator(mode="after")
    def valid_range(self):
        if self.due_from and self.due_to and self.due_from > self.due_to:
            raise ValueError("Invalid due range")
        return self


class CommitmentPage(BaseModel):
    items: list[CommitmentView]
    next_offset: int | None


class HistoryPage(BaseModel):
    items: list[AuditEvent]
    next_offset: int | None


class ActionContext(BaseModel):
    commitment: CommitmentState
    role: Role
    assignee_valid: bool = False
    previous_request: dict | None = Field(default=None, repr=False)
    previous_result: ActionResult | None = Field(default=None, repr=False)


class CommitmentError(Exception):
    """Fixed public category; never include remote error bodies or request data."""
    def __init__(self, code: str, status: int):
        self.code, self.status = code, status
        super().__init__(code)
