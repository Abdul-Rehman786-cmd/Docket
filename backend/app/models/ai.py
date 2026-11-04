from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.messages import MessageRef

DatabaseAction = Literal["store_only", "query_only", "none", "store_and_query"]


class ExtractedCommitment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    description: str = Field(min_length=1, max_length=2000)
    owner_name: str | None = Field(default=None, max_length=256)
    due_at: str | None = Field(default=None, max_length=128)


class ExtractedDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    topic: str | None = Field(default=None, max_length=256)
    summary: str = Field(min_length=1, max_length=2000)


class ExtractedMemory(BaseModel):
    model_config = ConfigDict(extra="ignore")

    memory_type: Literal["fact", "decision", "commitment"] = "fact"
    content: str = Field(min_length=1, max_length=2000)


class LLMDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: DatabaseAction = "none"
    search_query: str | None = Field(default=None, max_length=500)
    commitments: list[ExtractedCommitment] = Field(default_factory=list)
    decisions: list[ExtractedDecision] = Field(default_factory=list)
    memories: list[ExtractedMemory] = Field(default_factory=list)
    answer: str | None = Field(default=None, max_length=4000)


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    request_id: UUID
    question: str | None = Field(default=None, min_length=1, max_length=4000, repr=False)
    source_message_ref: MessageRef | None = None

    @model_validator(mode="after")
    def one_question_source(self):
        if (self.question is None) == (self.source_message_ref is None):
            raise ValueError("Provide a question or a retained message reference")
        if self.question is not None and not self.question.strip():
            raise ValueError("Question cannot be blank")
        return self


class AIResponse(BaseModel):
    request_id: UUID
    source_message_ref: MessageRef | None
    created_at: datetime
    status: Literal["queued", "waiting", "answered", "failed"] = "queued"
    question: str = Field(repr=False)
    answer: str | None = Field(default=None, repr=False)
    db_action: DatabaseAction | None = None
    stored_counts: dict[str, int] | None = None
    error: str | None = None
