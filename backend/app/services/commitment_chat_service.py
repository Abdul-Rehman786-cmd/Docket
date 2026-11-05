"""AI proposes; evidence gate and lifecycle service decide. No direct task writes."""
from uuid import NAMESPACE_URL, uuid5
import json
from datetime import datetime
from app.core.message_logging import message_text_for_log
from app.models.commitment_chat import ChatExecution, ChatOutcome
from app.models.commitments import Action, CommitmentError, CompleteRequest, CancelRequest, ReassignRequest, RescheduleRequest
from app.services.commitment_chat_evidence import validate_proposal
from app.services.commitment_service import CommitmentService

REQUESTS={'complete':CompleteRequest,'cancel':CancelRequest,'reassign':ReassignRequest,'reschedule':RescheduleRequest}


class CommitmentChatService:
    def __init__(self, repository, lifecycle=None):
        self.repository=repository
        self.lifecycle=lifecycle or CommitmentService(repository)

    async def prepare(self,settings,meta,body):
        if not settings.automatic_commitments_enabled or not meta.get('adapter_verified') or not meta.get('durable'):
            return None
        if not meta.get('switch_author_id') or meta.get('timestamp_ms') is None:
            return None
        plan=await self.repository.prepare_chat(settings.switch_tenant_id,meta,body)
        if plan is not None:
            source=plan.source
            if (source.scope.tenant_id!=settings.switch_tenant_id or source.switch_message_id!=meta['switch_message_id']
                or source.switch_author_id!=meta['switch_author_id'] or source.body!=body
                or source.thread_id!=meta.get('thread_id') or source.external_author_id!=meta.get('external_author_id')
                or any(getattr(source,field)!=meta.get(field) for field in ('external_message_id','workspace_id','channel_id'))
                or source.occurred_at!=datetime.fromisoformat(meta['occurred_at'].replace('Z','+00:00'))):
                raise CommitmentError('invalid_storage_result',503)
            # Legacy stored text is data too; never send configured secrets to the model.
            def safe(text):
                return json.loads(message_text_for_log(text,settings)) if text is not None else None
            plan=plan.model_copy(update={
                'candidates':[c.model_copy(update={'description':safe(c.description),'owner_name':(safe(c.owner_name) or '')[:256] or None}) for c in plan.candidates],
                'members':[m.model_copy(update={'label':(safe(m.label) or '')[:256] or None}) for m in plan.members]})
        return plan

    async def execute(self,plan,proposals,source_ref):
        def outcome(status,reason,**kwargs):
            return ChatOutcome(status=status,reason=reason,source_ref=source_ref,**kwargs)
        if plan is None: return outcome('unauthorized','chat_identity_or_policy_unavailable')
        if len(proposals)!=1: return outcome('ambiguous','no_unique_proposal')
        proposal=proposals[0]
        try:
            candidate,fields=validate_proposal(plan,proposal)
        except PermissionError:
            return outcome('unauthorized','sender_or_handoff_denied')
        except ValueError:
            return outcome('ambiguous','evidence_or_target_unresolved')
        key=uuid5(NAMESPACE_URL,f'commitment-chat:{plan.source.scope.tenant_id}:{plan.source.scope.room_id}:'
                  f'{plan.source.switch_message_id}:{candidate.id}:{proposal.action}')
        request=REQUESTS[proposal.action](idempotency_key=key,expected_version=candidate.version,**fields)
        execution=ChatExecution(source=plan.source,policy=plan.policy)
        try:
            result=await self.lifecycle.perform_chat(execution,candidate.id,Action(proposal.action),request)
        except CommitmentError as error:
            if error.status>=500: raise  # Existing durable worker handles bounded technical retries.
            return outcome('conflict' if error.status==409 else 'unauthorized','lifecycle_'+error.code,
                           commitment_id=candidate.id)
        return outcome('applied' if result.changed else 'no_change','lifecycle_result',
                       commitment_id=candidate.id,event_id=result.event.id)
