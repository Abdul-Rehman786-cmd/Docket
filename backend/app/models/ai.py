from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.messages import MessageRef


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
    error: str | None = None
