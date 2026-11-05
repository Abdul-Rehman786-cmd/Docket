"""Supabase lifecycle adapter: one RPC per atomic mutation; never log remote bodies."""

import httpx
from pydantic import ValidationError

from app.core.config import get_settings
from app.core.http_clients import service_client
from app.models.commitments import ActionContext, ActionResult, AuditEvent, CommitmentError, CommitmentState
from app.repositories.record_repository import _rest_url, _supabase_headers
from app.core.feature_logging import feature_event

PUBLIC_ERRORS = {
    "not_found": 404, "forbidden_action": 403, "stale_version": 409,
    "invalid_transition": 409, "idempotency_conflict": 409,
    "invalid_assignee": 422, "invalid_action_payload": 422,
}


class SupabaseCommitmentRepository:
    def __init__(self, settings, client=None):
        self.settings, self.client = settings, client

    async def _rpc(self, name, payload):
        if not self.settings.supabase_configured:
            raise CommitmentError("commitment_storage_unavailable", 503)
        async def call(client):
            try:
                response = await client.post(_rest_url(self.settings, "rpc/" + name),
                    headers=_supabase_headers(self.settings), json=payload, timeout=15)
                if response.status_code >= 400:
                    # Only whitelisted fixed categories are public. No URL, detail, hint or body logging.
                    try:
                        body = response.json()
                    except ValueError:
                        body = {}
                    category = body.get("message") if isinstance(body, dict) else None
                    code = body.get("code") if isinstance(body, dict) else None
                    feature_event('storage','blocked' if code=='PGRST202' else 'failed',
                                  reason='rpc_not_available' if code=='PGRST202' else 'database_request_rejected',status=response.status_code)
                    if category in PUBLIC_ERRORS and code == "PT" + str(PUBLIC_ERRORS[category]):
                        raise CommitmentError(category, PUBLIC_ERRORS[category])
                    raise CommitmentError("commitment_storage_unavailable", 503)
                return response.json()
            except CommitmentError:
                raise
            except (httpx.HTTPError, ValueError, TypeError):
                raise CommitmentError("commitment_storage_unavailable", 503) from None
        if self.client is not None:
            return await call(self.client)
        async with service_client("database") as client:
            return await call(client)

    @staticmethod
    def _scope(scope, actor):
        return {"p_tenant_id": str(scope.tenant_id), "p_room_id": str(scope.room_id), "p_actor_id": actor.subject_id}

    @staticmethod
    def _validate_scope(record, scope, commitment_id=None):
        if record.tenant_id != scope.tenant_id or record.room_id != scope.room_id or (
            commitment_id is not None and record.id != commitment_id
        ):
            raise CommitmentError("invalid_storage_result", 503)

    async def _read(self, scope, actor, mode, commitment_id=None, options=None):
        return await self._rpc("read_commitment_lifecycle", {
            **self._scope(scope, actor), "p_mode": mode,
            "p_commitment_id": str(commitment_id) if commitment_id else None, "p_options": options or {},
        })

    async def context(self, scope, actor, commitment_id, request=None):
        options = {}
        if request is not None:
            options["idempotency_key"] = str(request.idempotency_key)
            options["assignee_id"] = getattr(request, "assignee_id", None)
        try:
            result = ActionContext.model_validate(await self._read(scope, actor, "context", commitment_id, options))
            self._validate_scope(result.commitment, scope, commitment_id)
            return result
        except ValidationError:
            raise CommitmentError("invalid_storage_result", 503) from None

    async def list(self, scope, actor, filters):
        result = await self._read(scope, actor, "list", options=filters.model_dump(mode="json", exclude_none=True))
        try:
            rows = [CommitmentState.model_validate(row) for row in result["items"]]
            for row in rows:
                self._validate_scope(row, scope)
            return rows, result["next_offset"]
        except (KeyError, TypeError, ValidationError):
            raise CommitmentError("invalid_storage_result", 503) from None

    async def history(self, scope, actor, commitment_id, limit, offset):
        result = await self._read(scope, actor, "history", commitment_id, {"limit": limit, "offset": offset})
        try:
            rows = [AuditEvent.model_validate(row) for row in result["items"]]
            for row in rows:
                if (row.tenant_id, row.room_id, row.commitment_id) != (scope.tenant_id, scope.room_id, commitment_id):
                    raise CommitmentError("invalid_storage_result", 503)
            return rows, result["next_offset"]
        except (KeyError, TypeError, ValidationError):
            raise CommitmentError("invalid_storage_result", 503) from None

    async def apply_action(self, scope, actor, commitment_id, action, request, at):
        # PostgreSQL uses its transaction timestamp; at is for fake/local adapters' controllable clock.
        result = await self._rpc("apply_commitment_action", {
            **self._scope(scope, actor), "p_commitment_id": str(commitment_id),
            "p_action": action.value, "p_expected_version": request.expected_version,
            "p_idempotency_key": str(request.idempotency_key), "p_payload": request.payload(),
        })
        try:
            parsed = ActionResult.model_validate(result)
            self._validate_scope(parsed.commitment, scope, commitment_id)
            event = parsed.event
            if (event.tenant_id, event.room_id, event.commitment_id, event.actor_id, event.idempotency_key, event.action) != (
                scope.tenant_id, scope.room_id, commitment_id, actor.subject_id, request.idempotency_key, action
            ):
                raise CommitmentError("invalid_storage_result", 503)
            return parsed
        except ValidationError:
            raise CommitmentError("invalid_storage_result", 503) from None

    async def prepare_chat(self,tenant_id,metadata,body):
        from app.models.commitment_chat import ChatPlan
        source={key:metadata.get(key) for key in ('switch_message_id','switch_author_id','external_author_id',
                                                   'thread_id','occurred_at','external_message_id','workspace_id','channel_id')}
        if metadata.get('policy_mode')=='open_mentions':
            source.update({key:metadata.get(key) for key in ('policy_mode','registered_agent_id','registered_agent_name','sender_is_bot')})
        result=await self._rpc('prepare_commitment_chat',dict(p_tenant_id=str(tenant_id),
            p_switch_room_id=metadata['room_id'],p_source={**source,'body':body}))
        try:
            return ChatPlan.model_validate(result) if result is not None else None
        except ValidationError:
            raise CommitmentError('invalid_storage_result',503) from None

    async def apply_chat_action(self,execution,commitment_id,action,request,at):
        scope,actor=execution.source.scope,execution.source.actor
        result=await self._rpc('apply_commitment_chat_action',{
            **self._scope(scope,actor),'p_commitment_id':str(commitment_id),'p_action':action.value,
            'p_expected_version':request.expected_version,'p_idempotency_key':str(request.idempotency_key),
            'p_payload':request.payload(),'p_chat_source_id':str(execution.source.id),
            'p_backend_actor':execution.backend_actor})
        try:
            parsed=ActionResult.model_validate(result)
            self._validate_scope(parsed.commitment,scope,commitment_id)
            if (parsed.event.actor_id!=actor.subject_id or parsed.event.source_evidence_id!=execution.source.id
                or parsed.event.backend_actor!=execution.backend_actor or parsed.event.action!=action
                or parsed.event.idempotency_key!=request.idempotency_key
                or parsed.event.commitment_id!=commitment_id or parsed.event.tenant_id!=scope.tenant_id
                or parsed.event.room_id!=scope.room_id):
                raise CommitmentError('invalid_storage_result',503)
            return parsed
        except ValidationError:
            raise CommitmentError('invalid_storage_result',503) from None

    async def create_chat(self,execution,key,description,due_at,at):
        source=execution.source
        result=await self._rpc('create_owner_commitment',{
            **self._scope(source.scope,source.actor),'p_chat_source_id':str(source.id),
            'p_idempotency_key':str(key),'p_description':description,
            'p_due_at':due_at.isoformat() if due_at else None})
        try:
            parsed=ActionResult.model_validate(result)
            self._validate_scope(parsed.commitment,source.scope)
            if (parsed.commitment.owner_switch_user_id!=source.actor.subject_id or
                parsed.event.actor_id!=source.actor.subject_id or parsed.event.source_evidence_id!=source.id or
                parsed.event.backend_actor!=execution.backend_actor or parsed.event.idempotency_key!=key or
                parsed.event.action.value!='create' or parsed.event.commitment_id!=parsed.commitment.id or
                parsed.event.tenant_id!=source.scope.tenant_id or parsed.event.room_id!=source.scope.room_id):
                raise CommitmentError('invalid_storage_result',503)
            return parsed
        except ValidationError:
            raise CommitmentError('invalid_storage_result',503) from None


def commitment_repository():
    return SupabaseCommitmentRepository(get_settings())
