"""AI proposals are untrusted. ChatPlan/Execution are backend-only contracts."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from app.models.commitments import Actor, CommitmentState, Role, Scope, SubjectId


class TaskChangeProposal(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    action: Literal['create', 'start', 'complete', 'cancel', 'reassign', 'reschedule', 'list']
    commitment_id: UUID | None = None
    source_message_id: SubjectId
    evidence: str = Field(min_length=1, max_length=4000, repr=False)
    clarification_required: bool
    assignee_id: SubjectId | None = None
    due_at: AwareDatetime | None = None
    description: str | None = Field(default=None, min_length=1, max_length=2000)


class ChatPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)
    timezone: str | None = None
    accept_pending_owner_completion: bool = False
    handoff: Literal['admin_only'] = 'admin_only'
    mode: Literal['mapped', 'open_mentions'] = 'mapped'


class ChatMember(BaseModel):
    subject_id: SubjectId
    label: str | None = Field(default=None, max_length=256)
    external_author_id: str | None = None


class ChatSource(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)
    id: UUID
    scope: Scope
    switch_message_id: SubjectId
    switch_author_id: SubjectId
    external_author_id: str | None = None
    actor: Actor
    external_message_id: str | None = None
    workspace_id: str | None = None
    channel_id: str | None = None
    body: str = Field(max_length=64000, repr=False)
    occurred_at: AwareDatetime
    thread_id: str | None = None
    registered_agent_name: str | None = None


class ChatPlan(BaseModel):
    source: ChatSource
    role: Role
    policy: ChatPolicy
    candidates: list[CommitmentState] = Field(max_length=21)
    members: list[ChatMember] = Field(max_length=101)
    truncated: bool = False


class ChatExecution(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    backend_actor: Literal['switch-ingestion-worker'] = 'switch-ingestion-worker'
    source: ChatSource
    policy: ChatPolicy


class ChatOutcome(BaseModel):
    status: Literal['applied','no_change','ambiguous','unauthorized','conflict','failed']
    reason: str  # Fixed safe categories only, never model output.
    source_ref: str | None = None
    commitment_id: UUID | None = None
    event_id: UUID | None = None
    reply: str | None = Field(default=None, max_length=4000, repr=False)
