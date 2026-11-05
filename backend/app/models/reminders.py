"""Reminder contracts. Policies share the existing backend-managed room policy store."""
from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import AwareDatetime,BaseModel,ConfigDict,Field,model_validator
from app.models.commitments import CommitmentState,Scope,SubjectId


class ReminderPolicy(BaseModel):
    model_config=ConfigDict(frozen=True,extra='forbid')
    version:int=Field(default=1,ge=1)
    enabled:bool=False
    timezone:str|None=None
    lead_minutes:int=Field(default=30,ge=0,le=10080)
    cadence_minutes:int=Field(default=1440,ge=60,le=43200)
    max_followups:int=Field(default=3,ge=0,le=10)
    minimum_gap_minutes:int=Field(default=30,ge=1,le=1440)
    room_gap_seconds:int=Field(default=60,ge=15,le=3600)
    quiet_start:int=Field(default=22,ge=0,le=23)
    quiet_end:int=Field(default=8,ge=0,le=23)
    delivery_mode:Literal['source_thread','room']='source_thread'


class ReminderHistory(BaseModel):
    upcoming_sent:bool=False
    overdue_sent:int=Field(default=0,ge=0)
    last_sent_at:AwareDatetime|None=None
    last_overdue_at:AwareDatetime|None=None
    last_room_sent_at:AwareDatetime|None=None
    blocking_delivery:bool=False


class ReminderWork(BaseModel):
    commitment:CommitmentState
    policy:ReminderPolicy
    history:ReminderHistory=Field(default_factory=ReminderHistory)
    room_enabled:bool
    assignee_is_member:bool
    switch_room_id:str
    platform:str|None=None
    thread_id:str|None=None


class ReminderIntention(BaseModel):
    model_config=ConfigDict(frozen=True,extra='forbid')
    occurrence_key:UUID
    tenant_id:UUID
    room_id:UUID
    commitment_id:UUID
    recipient_id:SubjectId
    commitment_version:int=Field(ge=1)
    policy_version:int=Field(ge=1)
    kind:Literal['upcoming','overdue']
    ordinal:int=Field(ge=0,le=10)
    occurrence_base:AwareDatetime
    scheduled_for:AwareDatetime


class ReminderDelivery(BaseModel):
    id:UUID
    intention:ReminderIntention
    status:Literal['pending','sending','sent','failed','superseded','ambiguous']
    attempts:int=0
    claim_token:UUID|None=None
    lease_until:AwareDatetime|None=None
    dispatch_started_at:AwareDatetime|None=None
    sent_at:AwareDatetime|None=None
    next_attempt_at:AwareDatetime|None=None
    switch_reply_message_id:str|None=None
    error_code:str|None=None


class DispatchContext(BaseModel):
    delivery:ReminderDelivery
    work:ReminderWork


class NotificationReceipt(BaseModel):
    message_id:str=Field(min_length=1,max_length=512,pattern=r'^\S+$')


class NotificationEnvelope(BaseModel):
    room_id:str
    body:str
    thread_id:str|None=None


class NotificationFailure(Exception):
    def __init__(self,kind:Literal['transient','permanent','ambiguous'],category:str):
        self.kind,self.category=kind,category
        super().__init__(category)
