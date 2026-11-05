"""AI proposes; evidence gate and lifecycle service decide. No direct task writes."""
from uuid import NAMESPACE_URL, uuid5
import json
from datetime import datetime
from app.core.message_logging import message_text_for_log
from app.models.commitment_chat import ChatExecution, ChatOutcome
from app.models.commitments import Action, CommitmentError, StartRequest, CompleteRequest, CancelRequest, ReassignRequest, RescheduleRequest
from app.services.commitment_chat_evidence import validate_proposal, validate_creation, own_task_query
from app.core.chat_participation import explicit_agent_mention, participant_id
from app.services.commitment_service import CommitmentService
from app.core.feature_logging import feature_event

REQUESTS={'start':StartRequest,'complete':CompleteRequest,'cancel':CancelRequest,'reassign':ReassignRequest,'reschedule':RescheduleRequest}


class CommitmentChatService:
    def __init__(self, repository, lifecycle=None):
        self.repository=repository
        self.lifecycle=lifecycle or CommitmentService(repository)

    async def prepare_open(self,settings,meta,body):
        if not meta or meta.get('room_id') not in settings.allowed_switch_rooms: return None
        if not explicit_agent_mention(body,meta,settings):
            feature_event('chat_commitment','blocked',reference=meta.get('switch_message_id'),reason='explicit_mention_required')
            return None
        return await self._prepare(settings,{**meta,'policy_mode':'open_mentions'},body)

    async def prepare(self,settings,meta,body):
        """Legacy trusted integration contract; not used by the open chat worker."""
        return await self._prepare(settings,meta,body)

    async def _prepare(self,settings,meta,body):
        if not settings.automatic_commitments_enabled or not meta.get('adapter_verified') or not meta.get('durable'):
            feature_event('chat_commitment','disabled' if not settings.automatic_commitments_enabled else 'blocked',
                          reason='flag_disabled' if not settings.automatic_commitments_enabled else 'adapter_not_verified')
            return None
        if not meta.get('switch_author_id') or meta.get('timestamp_ms') is None:
            feature_event('chat_commitment','blocked',reason='sender_timestamp_unavailable')
            return None
        plan=await self.repository.prepare_chat(settings.switch_tenant_id,meta,body)
        if plan is not None:
            if meta.get('policy_mode')=='open_mentions' and (plan.policy.mode!='open_mentions' or plan.role!='member'
                or plan.source.registered_agent_name!=meta.get('registered_agent_name') or plan.source.actor.subject_id!=
                    participant_id(settings.switch_tenant_id,plan.source.scope.room_id,meta['switch_author_id'])):
                raise CommitmentError('invalid_storage_result',503)
            if meta.get('policy_mode')=='open_mentions' and any(
                (c.tenant_id,c.room_id,c.owner_switch_user_id)!=(plan.source.scope.tenant_id,
                    plan.source.scope.room_id,plan.source.actor.subject_id) for c in plan.candidates):
                raise CommitmentError('invalid_storage_result',503)
            feature_event('chat_commitment','prepared',reference=meta['switch_message_id'],candidates=len(plan.candidates))
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
        else:feature_event('chat_commitment','blocked',reference=meta.get('switch_message_id'),reason='mapping_or_policy_unavailable')
        return plan

    async def execute(self,plan,proposals,source_ref):
        def outcome(status,reason,**kwargs):
            return ChatOutcome(status=status,reason=reason,source_ref=source_ref,**kwargs)
        if plan is None:
            feature_event('chat_commitment','blocked',reference=source_ref,reason='mapping_or_policy_unavailable')
            return outcome('unauthorized','chat_identity_or_policy_unavailable')
        if len(proposals)!=1:
            feature_event('chat_commitment','ambiguous',reference=source_ref,reason='no_unique_proposal',proposed=len(proposals))
            return outcome('ambiguous','no_unique_proposal')
        proposal=proposals[0]
        if proposal.action=='list':
            if not own_task_query(plan,proposal): return outcome('ambiguous','evidence_or_target_unresolved')
            if any((c.tenant_id,c.room_id,c.owner_switch_user_id)!=(plan.source.scope.tenant_id,
                plan.source.scope.room_id,plan.source.actor.subject_id) for c in plan.candidates):
                return outcome('unauthorized','sender_or_handoff_denied')
            rows=plan.candidates[:20]
            reply='Your open tasks:\n'+'\n'.join(f'- {c.description} ({c.task_status}; due {c.due_at.isoformat() if c.due_at else "unscheduled"})' for c in rows)
            if not rows: reply='You have no open tasks in this room.'
            if plan.truncated: reply+='\nShowing the first 20 tasks; narrow your question.'
            feature_event('commitment','succeeded',action='list',reference=source_ref,count=len(rows))
            return outcome('no_change','own_tasks',reply=reply[:4000])
        if proposal.action=='create':
            feature_event('commitment','requested',action='create',reference=source_ref)
            try:
                description,due=validate_creation(plan,proposal)
            except ValueError:
                feature_event('chat_commitment','ambiguous',action='create',reference=source_ref,reason='unsupported_evidence')
                return outcome('ambiguous','evidence_or_target_unresolved')
            key=uuid5(NAMESPACE_URL,f'commitment-chat-create:{plan.source.scope.tenant_id}:{plan.source.scope.room_id}:{plan.source.switch_message_id}')
            try:
                result=await self.lifecycle.create_chat(ChatExecution(source=plan.source,policy=plan.policy),key,description,due)
            except CommitmentError as error:
                if error.status>=500: raise
                return outcome('conflict' if error.status==409 else 'unauthorized','lifecycle_'+error.code)
            feature_event('commitment','persisted',action='create',reference=source_ref,task=result.commitment.id,version=result.commitment.version)
            return outcome('applied','owner_commitment_created',commitment_id=result.commitment.id,event_id=result.event.id,
                reply='Created your commitment: '+result.commitment.description)
        feature_event('chat_commitment','proposed',reference=source_ref,task=proposal.commitment_id,action=proposal.action)
        try:
            candidate,fields=validate_proposal(plan,proposal)
        except PermissionError as error:
            feature_event('chat_commitment','blocked',reference=source_ref,task=proposal.commitment_id,reason=str(error))
            return outcome('unauthorized','sender_or_handoff_denied')
        except ValueError as error:
            feature_event('chat_commitment','ambiguous',reference=source_ref,task=proposal.commitment_id,reason=str(error))
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
