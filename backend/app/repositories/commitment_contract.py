from __future__ import annotations

"""Storage-independent port; apply_action must be one atomic database operation."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.models.commitments import Action, ActionContext, ActionRequest, ActionResult, Actor, AuditEvent, CommitmentState, Filters, Scope


class CommitmentRepository(Protocol):
    async def context(self, scope: Scope, actor: Actor, commitment_id: UUID,
                      request: ActionRequest | None = None) -> ActionContext: ...

    async def list(self, scope: Scope, actor: Actor, filters: Filters) -> tuple[list[CommitmentState], int | None]: ...

    async def history(self, scope: Scope, actor: Actor, commitment_id: UUID,
                      limit: int, offset: int) -> tuple[list[AuditEvent], int | None]: ...

    async def apply_action(self, scope: Scope, actor: Actor, commitment_id: UUID,
                           action: Action, request: ActionRequest, at: datetime) -> ActionResult: ...

    async def apply_chat_action(self, execution, commitment_id: UUID, action: Action,
                                request: ActionRequest, at: datetime) -> ActionResult: ...

    async def prepare_chat(self, tenant_id: UUID, metadata: dict, body: str): ...

    async def create_chat(self, execution, key: UUID, description: str, due_at: datetime | None,
                          at: datetime) -> ActionResult: ...
