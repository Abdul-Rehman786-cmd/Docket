from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, StrictBool

OpaqueId = Annotated[str, StringConstraints(min_length=1, max_length=512, pattern=r"^\S+$")]
MessageRef = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{24}$")]


class IncomingMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    tenant_id: UUID
    room_id: OpaqueId
    message_id: OpaqueId
    sender: OpaqueId = Field(repr=False)
    sender_name: str | None = Field(default=None, max_length=256, repr=False)
    body: str = Field(default="", max_length=64000, repr=False)
    addressed: bool = False
    timestamp_ms: int | None = Field(default=None, ge=0, le=253402300799000)
    thread_id: OpaqueId | None = None
    workspace_id: OpaqueId | None = None
    channel_id: OpaqueId | None = None
    external_message_id: OpaqueId | None = None
    external_author_id: OpaqueId | None = None
    registered_agent_id: OpaqueId | None = None
    registered_agent_name: str | None = Field(default=None, max_length=128)
    mentioned_agent_ids: list[OpaqueId] | None = Field(default=None, max_length=20)
    sender_is_bot: StrictBool | None = None


class MessageReceipt(BaseModel):
    status: Literal["received"] = "received"
    message_ref: MessageRef
    received_at: datetime
    duplicate: bool = False


class DeliveryStatistics(BaseModel):
    received_count: int
    duplicate_count: int
    last_message_ref: MessageRef | None
    last_received_at: datetime | None


class MessageStatus(BaseModel):
    sender_name: str | None = Field(repr=False)
    message: str | None = Field(repr=False)


class MessagePreview(BaseModel):
    message_ref: MessageRef
    received_at: datetime
    sender_name: str = Field(repr=False)
    text: str = Field(repr=False)
    sender_kind: Literal["message", "ai"] = "message"
    thread_ref: MessageRef | None = None
