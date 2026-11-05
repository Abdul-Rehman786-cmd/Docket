"""Backend-only scoped RPCs. Each mutation/claim is its own database transaction."""
from pydantic import ValidationError
from app.models.commitments import CommitmentError
from app.models.reminders import ReminderWork,ReminderDelivery,DispatchContext
from app.repositories.commitment_repository import SupabaseCommitmentRepository


class SupabaseReminderRepository(SupabaseCommitmentRepository):
    def scope(self):
        if not self.settings.switch_tenant_id or not self.settings.allowed_switch_rooms:
            raise CommitmentError('reminder_scope_unavailable',503)
        return {'p_tenant_id':str(self.settings.switch_tenant_id),'p_switch_room_ids':sorted(self.settings.allowed_switch_rooms)}

    async def call(self,name,**fields):
        return await self._rpc(name,{**self.scope(),**fields})

    def validate_scope(self,tenant_id,room=None):
        if tenant_id!=self.settings.switch_tenant_id or (room and room not in self.settings.allowed_switch_rooms):
            raise CommitmentError('invalid_storage_result',503)

    async def work(self,limit,after=None):
        result=await self.call('read_reminder_work',p_limit=limit,p_after=after)
        try:
            rows=[ReminderWork.model_validate(row) for row in result['items']]
            for row in rows:self.validate_scope(row.commitment.tenant_id,row.switch_room_id)
            return rows,result['next_cursor']
        except (ValidationError,KeyError,TypeError):raise CommitmentError('invalid_storage_result',503) from None

    async def persist(self,intention):
        self.validate_scope(intention.tenant_id)
        return await self.call('persist_reminder_intention',p_intention=intention.model_dump(mode='json')) is True

    async def claim(self,limit):
        result=await self.call('claim_reminder_deliveries',p_limit=limit)
        try:
            rows=[ReminderDelivery.model_validate(row) for row in result]
            for row in rows:self.validate_scope(row.intention.tenant_id)
            return rows
        except (ValidationError,TypeError):raise CommitmentError('invalid_storage_result',503) from None

    def reference(self,delivery):
        self.validate_scope(delivery.intention.tenant_id)
        return {'p_delivery_id':str(delivery.id),'p_claim_token':str(delivery.claim_token)}

    async def context(self,delivery):
        result=await self.call('read_reminder_dispatch',**self.reference(delivery))
        if result is None:return None
        try:
            parsed=DispatchContext.model_validate(result)
            self.validate_scope(parsed.work.commitment.tenant_id,parsed.work.switch_room_id)
            if (parsed.delivery.id!=delivery.id or parsed.delivery.claim_token!=delivery.claim_token or parsed.delivery.intention!=delivery.intention
                or (parsed.work.commitment.tenant_id,parsed.work.commitment.room_id,parsed.work.commitment.id)!=(delivery.intention.tenant_id,delivery.intention.room_id,delivery.intention.commitment_id)):
                raise CommitmentError('invalid_storage_result',503)
            return parsed
        except ValidationError:raise CommitmentError('invalid_storage_result',503) from None

    async def begin(self,delivery,body):
        return await self.call('begin_reminder_dispatch',**self.reference(delivery),p_body=body) is True

    async def defer(self,delivery,until):
        return await self.call('defer_reminder_delivery',**self.reference(delivery),p_until=until.isoformat() if until else None) is True

    async def finish(self,delivery,outcome,message_id=None,error=None):
        saved=await self.call('finish_reminder_delivery',**self.reference(delivery),p_outcome=outcome,p_message_id=message_id,p_error=error)
        if saved is not True:raise CommitmentError('reminder_acknowledgement_not_saved',503)
