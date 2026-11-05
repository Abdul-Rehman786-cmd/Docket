"""Scoped human lifecycle API. Existing server-admin diagnostic reads remain separate."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.core.commitment_identity import resolve_commitment_actor
from app.models.commitments import (
    Action, ActionResult, Actor, ApproveRequest, CancelRequest, CommitmentPage,
    CommitmentView, CompleteRequest, Filters, HistoryPage, ReassignRequest,
    RejectRequest, RescheduleRequest, Scope, StartRequest,
)
from app.repositories.commitment_repository import commitment_repository
from app.services.commitment_service import CommitmentService

router = APIRouter(prefix="/api/v1/tenants/{tenant_id}/rooms/{room_id}/commitments", tags=["commitment lifecycle"])
Human = Annotated[Actor, Depends(resolve_commitment_actor)]


def service(repository=Depends(commitment_repository)):
    return CommitmentService(repository)


Service = Annotated[CommitmentService, Depends(service)]


@router.get("", response_model=CommitmentPage)
async def list_commitments(tenant_id: UUID, room_id: UUID, actor: Human, lifecycle: Service,
                           filters: Annotated[Filters, Query()]):
    return await lifecycle.list(Scope(tenant_id=tenant_id, room_id=room_id), actor, filters)


@router.get("/{commitment_id}", response_model=CommitmentView)
async def get_commitment(tenant_id: UUID, room_id: UUID, commitment_id: UUID, actor: Human, lifecycle: Service):
    return await lifecycle.get(Scope(tenant_id=tenant_id, room_id=room_id), actor, commitment_id)


@router.get("/{commitment_id}/history", response_model=HistoryPage)
async def history(tenant_id: UUID, room_id: UUID, commitment_id: UUID, actor: Human, lifecycle: Service,
                  limit: Annotated[int, Query(ge=1, le=100)] = 25,
                  offset: Annotated[int, Query(ge=0, le=10000)] = 0):
    return await lifecycle.history(Scope(tenant_id=tenant_id, room_id=room_id), actor, commitment_id, limit, offset)


@router.post("/{commitment_id}/approve", response_model=ActionResult)
async def approve(tenant_id: UUID, room_id: UUID, commitment_id: UUID, actor: Human, lifecycle: Service, body: ApproveRequest):
    return await lifecycle.perform(Scope(tenant_id=tenant_id, room_id=room_id), actor, commitment_id, Action.approve, body)


@router.post("/{commitment_id}/reject", response_model=ActionResult)
async def reject(tenant_id: UUID, room_id: UUID, commitment_id: UUID, actor: Human, lifecycle: Service, body: RejectRequest):
    return await lifecycle.perform(Scope(tenant_id=tenant_id, room_id=room_id), actor, commitment_id, Action.reject, body)


@router.post("/{commitment_id}/start", response_model=ActionResult)
async def start(tenant_id: UUID, room_id: UUID, commitment_id: UUID, actor: Human, lifecycle: Service, body: StartRequest):
    return await lifecycle.perform(Scope(tenant_id=tenant_id, room_id=room_id), actor, commitment_id, Action.start, body)


@router.post("/{commitment_id}/complete", response_model=ActionResult)
async def complete(tenant_id: UUID, room_id: UUID, commitment_id: UUID, actor: Human, lifecycle: Service, body: CompleteRequest):
    return await lifecycle.perform(Scope(tenant_id=tenant_id, room_id=room_id), actor, commitment_id, Action.complete, body)


@router.post("/{commitment_id}/cancel", response_model=ActionResult)
async def cancel(tenant_id: UUID, room_id: UUID, commitment_id: UUID, actor: Human, lifecycle: Service, body: CancelRequest):
    return await lifecycle.perform(Scope(tenant_id=tenant_id, room_id=room_id), actor, commitment_id, Action.cancel, body)


@router.post("/{commitment_id}/reassign", response_model=ActionResult)
async def reassign(tenant_id: UUID, room_id: UUID, commitment_id: UUID, actor: Human, lifecycle: Service, body: ReassignRequest):
    return await lifecycle.perform(Scope(tenant_id=tenant_id, room_id=room_id), actor, commitment_id, Action.reassign, body)


@router.post("/{commitment_id}/reschedule", response_model=ActionResult)
async def reschedule(tenant_id: UUID, room_id: UUID, commitment_id: UUID, actor: Human, lifecycle: Service, body: RescheduleRequest):
    return await lifecycle.perform(Scope(tenant_id=tenant_id, room_id=room_id), actor, commitment_id, Action.reschedule, body)
