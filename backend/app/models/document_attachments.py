"""Normalized trusted-adapter events. Attachment references never contain file bytes."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.messages import IncomingMessage


class SwitchAttachment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    filename: str = Field(default="attachment",max_length=512,repr=False)
    mimetype: str = Field(default="",max_length=200)
    size: int | None = Field(default=None,ge=0)
    mxc: str = Field(default="",max_length=1024,repr=False)
    msgtype: str = Field(default="m.file",max_length=50)

    @field_validator("size",mode="before")
    @classmethod
    def unknown_declared_size(cls,value):
        return value if type(value) is int and value>=0 else None


class AttachmentGroup(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(min_length=1,max_length=256)
    index: int = Field(ge=0,strict=True)
    total: int = Field(ge=1,le=100,strict=True)


class AttachmentMessage(IncomingMessage):
    attachments: list[SwitchAttachment] = Field(default_factory=list,max_length=100,repr=False)
    attachment_group: AttachmentGroup | None = None
    sender_kind: str | None = Field(default=None,max_length=50)
    platform: str | None = "discord"
    bridge_id: str | None = Field(default=None,max_length=512)

    @field_validator("platform",mode="before")
    @classmethod
    def default_platform(cls,value):
        return value if value in ("discord", "slack", "teams") else "discord"

    @field_validator("attachment_group",mode="before")
    @classmethod
    def malformed_group_is_ungrouped(cls,value):
        try:
            group=AttachmentGroup.model_validate(value)
            return group if group.index<group.total else None
        except (ValueError,TypeError):
            return None


class AttachmentReceipt(BaseModel):
    status: Literal["received"] = "received"
    message_ref: str
    received_at: str
    duplicate: bool = False
    request_ref: str | None = None
    processing_state: str


class AttachmentStatus(BaseModel):
    request_ref: str
    state: Literal["confirmation_requested","queued","processing","published","declined","expired","failed"]
    error: str | None = None
    filenames: list[str]
    documents: list[dict]
    reply_states: list[str]
    source_references: list[dict]
