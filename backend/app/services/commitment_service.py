"""Commitment domain rules. No LLM, database queries or notification delivery."""

from datetime import datetime, timezone
import json
from typing import Callable
from uuid import UUID

from app.models.commitments import (
    Action, ActionContext, ActionRequest, ActionResult, Actor, CommitmentError,
    CommitmentPage, CommitmentState, CommitmentView, Filters, HistoryPage,
    ReassignRequest, ReminderState, RescheduleRequest, Scope,
)
from app.repositories.commitment_contract import CommitmentRepository

MANAGEMENT = {Action.approve, Action.reject, Action.cancel, Action.reassign, Action.reschedule}
STATE_FIELDS = (
    "approval_status", "approval_by_switch_user_id", "approval_at", "task_status",
    "owner_switch_user_id", "owner_name", "due_at", "completed_at", "cancelled_at", "next_reminder_at", "version",
    "automated_acceptance_at", "automated_acceptance_source_id",
)


def reminder_state(record: CommitmentState) -> ReminderState:
    if record.approval_status != "approved":
        reason = record.approval_status
    elif record.task_status in ("completed", "cancelled"):
        reason = record.task_status
    elif not record.owner_switch_user_id:
        reason = "unassigned"
    elif record.due_at is None:
        reason = "no_deadline"
    else:
        reason = "eligible"
    return ReminderState(eligible=reason == "eligible", reason=reason, due_at=record.due_at,
                         assignee_id=record.owner_switch_user_id)


def utc_text(value: datetime | str | None) -> str:
    if value is None:
        return "unscheduled"
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An explicit timezone is required")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def summary_text(description: str, owner_name=None, owner_id=None, due_at=None,
                 approval_status="pending", task_status="not_started", automated_acceptance_at=None) -> str:
    owner = owner_id or ("unresolved: " + owner_name if owner_name else "unassigned")
    return (f"Commitment: {description}\nAssignee: {owner}\nApproval: {approval_status}"
            f"\nTask: {task_status}\nDue (UTC): {utc_text(due_at)}"
            + ('\nAcceptance: automated owner completion evidence' if automated_acceptance_at else ''))


def request_content(commitment_id: UUID, action: Action, request: ActionRequest, chat=None) -> dict:
    content = {"commitment_id": str(commitment_id), "action": action.value,
            "expected_version": request.expected_version, "payload": request.payload()}
    if chat is not None:
        content['chat_source_id'] = str(chat.source.id)
    return content


def snapshot(record: CommitmentState) -> dict:
    return record.model_dump(mode="json", include=set(STATE_FIELDS))


def authorize(context: ActionContext, actor: Actor, action: Action, chat=None) -> None:
    if chat is not None:
        record=context.commitment
        if (chat.backend_actor!='switch-ingestion-worker' or chat.source.actor!=actor or
            (record.tenant_id,record.room_id)!=(chat.source.scope.tenant_id,chat.source.scope.room_id)):
            raise CommitmentError('forbidden_action',403)
        if action not in (Action.complete,Action.cancel,Action.reschedule,Action.reassign):
            raise CommitmentError('forbidden_action',403)
        if context.role=='admin': return
        if action==Action.reassign or record.owner_switch_user_id!=actor.subject_id:
            raise CommitmentError('forbidden_action',403)
        return
    if action in MANAGEMENT:
        if context.role not in ("approver", "admin"):
            raise CommitmentError("forbidden_action", 403)
    elif context.commitment.owner_switch_user_id != actor.subject_id:
        # No implicit admin override for starting/completing another person's work.
        raise CommitmentError("forbidden_action", 403)


def decide(context: ActionContext, actor: Actor, action: Action, request: ActionRequest,
           at: datetime, chat=None) -> tuple[CommitmentState, bool] | ActionResult:
    """Used for preflight and fake-adapter contract tests; SQL revalidates under lock."""
    record = context.commitment
    authorize(context, actor, action, chat)
    content = request_content(record.id, action, request, chat)
    if context.previous_request is not None:
        if context.previous_request != content:
            raise CommitmentError("idempotency_conflict", 409)
        if context.previous_result is None:
            raise CommitmentError("invalid_storage_result", 503)
        return context.previous_result
    if request.expected_version != record.version:
        raise CommitmentError("stale_version", 409)
    updates = {}
    terminal = record.task_status in ("completed", "cancelled")
    equivalent_terminal = ((action == Action.complete and record.task_status == "completed")
                           or (action == Action.cancel and record.task_status == "cancelled"))
    if terminal and not equivalent_terminal:
        raise CommitmentError("invalid_transition", 409)
    if record.approval_status == "rejected" and action != Action.reject:
        raise CommitmentError("invalid_transition", 409)
    if action == Action.approve:
        if record.approval_status not in ("pending", "approved") or (record.approval_status == "pending" and record.task_status != "not_started"):
            raise CommitmentError("invalid_transition", 409)
        if record.approval_status == "pending":
            updates.update(approval_status="approved", approval_by_switch_user_id=actor.subject_id, approval_at=at)
    elif action == Action.reject:
        if record.approval_status not in ("pending", "rejected") or record.task_status != "not_started":
            raise CommitmentError("invalid_transition", 409)
        if record.approval_status == "pending":
            updates.update(approval_status="rejected", approval_by_switch_user_id=actor.subject_id, approval_at=at)
    elif action in (Action.start, Action.complete):
        pending_owner_evidence = (chat is not None and action==Action.complete and
            record.approval_status=='pending' and record.owner_switch_user_id==actor.subject_id and
            chat.policy.accept_pending_owner_completion)
        if record.approval_status != "approved" and not pending_owner_evidence:
            raise CommitmentError("invalid_transition", 409)
        desired = "in_progress" if action == Action.start else "completed"
        if record.task_status not in ("not_started", "in_progress", desired):
            raise CommitmentError("invalid_transition", 409)
        if record.task_status != desired:
            updates["task_status"] = desired
            if action == Action.complete:
                updates["completed_at"] = at
                if pending_owner_evidence:
                    updates.update(automated_acceptance_at=at,automated_acceptance_source_id=chat.source.id)
    elif action == Action.cancel:
        if record.task_status != "cancelled":
            updates.update(task_status="cancelled", cancelled_at=at)
    elif action == Action.reassign:
        if not isinstance(request, ReassignRequest) or not context.assignee_valid:
            raise CommitmentError("invalid_assignee", 422)
        if request.assignee_id != record.owner_switch_user_id:
            updates.update(owner_switch_user_id=request.assignee_id, owner_name=None)
    elif action == Action.reschedule:
        if not isinstance(request, RescheduleRequest):
            raise CommitmentError("invalid_action_payload", 422)
        if request.due_at != record.due_at:
            updates["due_at"] = request.due_at
    changed = bool(updates)
    if changed:
        updates.update(version=record.version + 1, updated_at=at, next_reminder_at=None)
    return CommitmentState.model_validate({**record.model_dump(), **updates}), changed


class CommitmentService:
    def __init__(self, repository: CommitmentRepository,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.repository, self.clock = repository, clock

    async def get(self, scope: Scope, actor: Actor, commitment_id: UUID) -> CommitmentView:
        context = await self.repository.context(scope, actor, commitment_id)
        return CommitmentView(commitment=context.commitment, reminder=reminder_state(context.commitment))

    async def list(self, scope: Scope, actor: Actor, filters: Filters) -> CommitmentPage:
        rows, next_offset = await self.repository.list(scope, actor, filters)
        return CommitmentPage(items=[CommitmentView(commitment=row, reminder=reminder_state(row)) for row in rows],
                              next_offset=next_offset)

    async def history(self, scope: Scope, actor: Actor, commitment_id: UUID, limit=25, offset=0) -> HistoryPage:
        rows, next_offset = await self.repository.history(scope, actor, commitment_id, limit, offset)
        return HistoryPage(items=rows, next_offset=next_offset)

    async def perform(self, scope: Scope, actor: Actor, commitment_id: UUID,
                      action: Action, request: ActionRequest) -> ActionResult:
        if request.reason:
            from app.core.config import get_settings
            from app.core.message_logging import message_text_for_log
            reason = json.loads(message_text_for_log(request.reason, get_settings()))[:1000]
            request = request.model_copy(update={"reason": reason})
        context = await self.repository.context(scope, actor, commitment_id, request)
        at = self.clock()
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("Clock must be timezone-aware")
        at = at.astimezone(timezone.utc)
        decide(context, actor, action, request, at)
        # Even a replay goes through the transaction, which rechecks current membership/permissions.
        # The preceding read is only preflight and is NOT part of the atomic write transaction.
        return await self.repository.apply_action(scope, actor, commitment_id, action, request, at)

    async def perform_chat(self, execution, commitment_id: UUID, action: Action, request: ActionRequest):
        """Internal worker entry. Repository must revalidate source/mapping/policy under lock.

        HTTP controllers never construct ChatExecution; AI output cannot supply it.
        """
        scope,actor=execution.source.scope,execution.source.actor
        context=await self.repository.context(scope,actor,commitment_id,request)
        at=self.clock()
        if at.tzinfo is None or at.utcoffset() is None: raise ValueError('Clock must be aware')
        decide(context,actor,action,request,at,execution)
        return await self.repository.apply_chat_action(execution,commitment_id,action,request,at)
