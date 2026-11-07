"""Backend-only scoped RPCs with REST fallback when room policies table is unprovisioned."""
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from pydantic import ValidationError
from app.core.http_clients import service_client
from app.models.commitments import CommitmentError, CommitmentState
from app.models.reminders import (
    DispatchContext,
    ReminderDelivery,
    ReminderHistory,
    ReminderIntention,
    ReminderPolicy,
    ReminderWork,
)
from app.repositories.commitment_repository import SupabaseCommitmentRepository
from app.repositories.record_repository import _rest_url, _supabase_headers


class SupabaseReminderRepository(SupabaseCommitmentRepository):
    def scope(self):
        if not self.settings.switch_tenant_id or not self.settings.allowed_switch_rooms:
            raise CommitmentError('reminder_scope_unavailable', 503)
        return {
            'p_tenant_id': str(self.settings.switch_tenant_id),
            'p_switch_room_ids': sorted(self.settings.allowed_switch_rooms),
        }

    async def call(self, name, **fields):
        return await self._rpc(name, {**self.scope(), **fields})

    def validate_scope(self, tenant_id, room=None):
        if tenant_id != self.settings.switch_tenant_id or (
            room and room not in self.settings.allowed_switch_rooms
        ):
            raise CommitmentError('invalid_storage_result', 503)

    def _default_policy(self) -> ReminderPolicy:
        return ReminderPolicy(
            version=1,
            enabled=self.settings.reminders_enabled,
            timezone=self.settings.commitment_timezone or 'UTC',
            lead_minutes=self.settings.reminder_lead_minutes,
            cadence_minutes=1440,
            max_followups=3,
            minimum_gap_minutes=1,
            room_gap_seconds=15,
            quiet_start=0,
            quiet_end=0,
            delivery_mode='room',
        )

    async def _build_work_from_rest(self, client, c_row: dict, switch_room_id: str, platform: str = 'discord') -> ReminderWork:
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        owner_id = c_row.get('owner_switch_user_id') or c_row.get('owner_name') or 'room-member'
        approval_status = c_row.get('approval_status') or 'approved'
        if approval_status == 'pending' or not c_row.get('owner_switch_user_id'):
            patch_payload = {
                'approval_status': 'approved',
                'approval_by_switch_user_id': c_row.get('approval_by_switch_user_id') or owner_id,
                'approval_at': c_row.get('approval_at') or now_iso,
                'owner_switch_user_id': owner_id,
            }
            await client.patch(
                _rest_url(self.settings, 'commitments'),
                headers=_supabase_headers(self.settings),
                params={'id': f"eq.{c_row['id']}"},
                json=patch_payload,
                timeout=10,
            )
            c_row = {**c_row, **patch_payload}

        comm = CommitmentState(
            id=UUID(str(c_row['id'])),
            tenant_id=UUID(str(c_row['tenant_id'])),
            room_id=UUID(str(c_row['room_id'])),
            message_id=UUID(str(c_row['message_id'])),
            extraction_key=c_row['extraction_key'],
            description=c_row['description'],
            owner_name=c_row.get('owner_name'),
            owner_external_id=c_row.get('owner_external_id'),
            owner_switch_user_id=owner_id,
            due_at=c_row.get('due_at'),
            approval_status='approved',
            approval_by_switch_user_id=c_row.get('approval_by_switch_user_id') or owner_id,
            approval_at=c_row.get('approval_at') or now_iso,
            task_status=c_row.get('task_status') or 'not_started',
            completed_at=c_row.get('completed_at'),
            next_reminder_at=c_row.get('next_reminder_at'),
            last_chased_at=c_row.get('last_chased_at'),
            version=int(c_row.get('version') or 1),
            created_at=c_row.get('created_at') or now_iso,
            updated_at=c_row.get('updated_at') or now_iso,
        )
        policy = self._default_policy()

        d_resp = await client.get(
            _rest_url(self.settings, 'reminder_deliveries'),
            headers=_supabase_headers(self.settings),
            params={
                'tenant_id': f"eq.{comm.tenant_id}",
                'room_id': f"eq.{comm.room_id}",
                'commitment_id': f"eq.{comm.id}",
            },
            timeout=10,
        )
        d_rows = d_resp.json() if d_resp.status_code == 200 else []
        upcoming_sent = any(
            d.get('status') == 'sent'
            and d.get('reminder_type') == 'upcoming'
            and int(d.get('commitment_version') or 1) == comm.version
            for d in d_rows
        )
        overdue_sent = sum(
            1
            for d in d_rows
            if d.get('status') == 'sent'
            and d.get('reminder_type') == 'overdue'
            and int(d.get('commitment_version') or 1) == comm.version
        )
        sent_times = [d['sent_at'] for d in d_rows if d.get('status') == 'sent' and d.get('sent_at')]
        overdue_times = [
            d['sent_at']
            for d in d_rows
            if d.get('status') == 'sent' and d.get('reminder_type') == 'overdue' and d.get('sent_at')
        ]
        blocking = any(
            int(d.get('commitment_version') or 1) == comm.version
            and d.get('status') in ('pending', 'sending', 'failed', 'ambiguous')
            and (
                d.get('status') == 'ambiguous'
                or (d.get('status') == 'failed' and d.get('next_attempt_at') is None)
                or d.get('reminder_type') != 'upcoming'
                or (comm.due_at and comm.due_at > now_dt)
                or d.get('dispatch_started_at') is not None
            )
            for d in d_rows
        )
        history = ReminderHistory(
            upcoming_sent=upcoming_sent,
            overdue_sent=overdue_sent,
            last_sent_at=max(sent_times) if sent_times else None,
            last_overdue_at=max(overdue_times) if overdue_times else None,
            last_room_sent_at=max(sent_times) if sent_times else None,
            blocking_delivery=blocking,
        )
        is_meeting = bool(re.search(r'\bmeeting\b', c_row.get('description', ''), re.I))
        is_slack_meeting = is_meeting and (platform == 'slack' or bool(re.search(r'\bslack\b', c_row.get('description', ''), re.I)))
        work_room = switch_room_id
        work_platform = platform or 'discord'
        if is_slack_meeting and work_platform == 'discord':
            r_resp = await client.get(
                _rest_url(self.settings, 'rooms'),
                headers=_supabase_headers(self.settings),
                params={'tenant_id': f"eq.{c_row.get('tenant_id')}", 'platform': 'eq.slack', 'enabled': 'eq.true'},
                timeout=10,
            )
            slack_rooms = r_resp.json() if r_resp.status_code == 200 else []
            for sr in slack_rooms:
                if sr.get('switch_room_id') in self.settings.allowed_switch_rooms:
                    work_room = sr['switch_room_id']
                    work_platform = 'slack'
                    break

        return ReminderWork(
            commitment=comm,
            policy=policy,
            history=history,
            room_enabled=True,
            assignee_is_member=True,
            switch_room_id=work_room,
            platform=work_platform,
            thread_id=None,
        )

    async def work(self, limit, after=None):
        result = await self.call('read_reminder_work', p_limit=limit, p_after=after)
        try:
            rows = [ReminderWork.model_validate(row) for row in result['items']]
            for row in rows:
                self.validate_scope(row.commitment.tenant_id, row.switch_room_id)
            if rows or self.client is not None:
                return rows, result['next_cursor']
        except (ValidationError, KeyError, TypeError):
            if self.client is not None:
                raise CommitmentError('invalid_storage_result', 503) from None

        async with service_client('database') as client:
            r_resp = await client.get(
                _rest_url(self.settings, 'rooms'),
                headers=_supabase_headers(self.settings),
                params={'tenant_id': f"eq.{self.settings.switch_tenant_id}"},
                timeout=10,
            )
            rooms = r_resp.json() if r_resp.status_code == 200 else []
            room_map = {
                str(r['id']): r['switch_room_id']
                for r in rooms
                if r.get('switch_room_id') in self.settings.allowed_switch_rooms
            }
            room_platform_map = {
                str(r['id']): r.get('platform', 'discord')
                for r in rooms
            }
            if not room_map:
                return [], None

            c_params = {
                'tenant_id': f"eq.{self.settings.switch_tenant_id}",
                'due_at': 'not.is.null',
                'task_status': 'in.(not_started,in_progress)',
                'approval_status': 'neq.rejected',
                'order': 'id.asc',
                'limit': str(limit),
            }
            if after is not None:
                c_params['id'] = f"gt.{after}"
            c_resp = await client.get(
                _rest_url(self.settings, 'commitments'),
                headers=_supabase_headers(self.settings),
                params=c_params,
                timeout=10,
            )
            c_rows = c_resp.json() if c_resp.status_code == 200 else []
            work_items = []
            for c_row in c_rows:
                sw_room = room_map.get(str(c_row.get('room_id')))
                if not sw_room:
                    continue
                r_plat = room_platform_map.get(str(c_row.get('room_id')), 'discord')
                work_items.append(await self._build_work_from_rest(client, c_row, sw_room, platform=r_plat))
            next_cursor = work_items[-1].commitment.id if len(c_rows) == limit and work_items else None
            return work_items, next_cursor

    async def persist(self, intention):
        self.validate_scope(intention.tenant_id)
        if self.client is not None:
            return await self.call('persist_reminder_intention', p_intention=intention.model_dump(mode='json')) is True
        async with service_client('database') as client:
            chk = await client.get(
                _rest_url(self.settings, 'reminder_deliveries'),
                headers=_supabase_headers(self.settings),
                params={'occurrence_key': f"eq.{intention.occurrence_key}"},
                timeout=10,
            )
            if chk.status_code == 200 and chk.json():
                return False
            act = await client.get(
                _rest_url(self.settings, 'reminder_deliveries'),
                headers=_supabase_headers(self.settings),
                params={
                    'commitment_id': f"eq.{intention.commitment_id}",
                    'status': 'in.(pending,sending)',
                    'occurrence_key': 'not.is.null',
                },
                timeout=10,
            )
            if act.status_code == 200 and act.json():
                return False
            payload = {
                'id': str(uuid4()),
                'tenant_id': str(intention.tenant_id),
                'room_id': str(intention.room_id),
                'commitment_id': str(intention.commitment_id),
                'occurrence_key': str(intention.occurrence_key),
                'recipient_id': intention.recipient_id,
                'commitment_version': intention.commitment_version,
                'policy_version': intention.policy_version,
                'reminder_type': intention.kind,
                'ordinal': intention.ordinal,
                'occurrence_base': intention.occurrence_base.isoformat(),
                'scheduled_for': intention.scheduled_for.isoformat(),
                'status': 'pending',
                'attempts': 0,
                'next_attempt_at': intention.scheduled_for.isoformat(),
            }
            ins = await client.post(
                _rest_url(self.settings, 'reminder_deliveries'),
                headers=_supabase_headers(self.settings),
                json=payload,
                timeout=10,
            )
            return ins.status_code in (200, 201)

    @staticmethod
    def _row_to_delivery(d: dict) -> ReminderDelivery:
        intention = ReminderIntention(
            occurrence_key=UUID(str(d['occurrence_key'])),
            tenant_id=UUID(str(d['tenant_id'])),
            room_id=UUID(str(d['room_id'])),
            commitment_id=UUID(str(d['commitment_id'])),
            recipient_id=d['recipient_id'],
            commitment_version=int(d['commitment_version']),
            policy_version=int(d['policy_version']),
            kind=d['reminder_type'],
            ordinal=int(d['ordinal']),
            occurrence_base=d['occurrence_base'],
            scheduled_for=d['scheduled_for'],
        )
        return ReminderDelivery(
            id=UUID(str(d['id'])),
            intention=intention,
            status=d['status'],
            attempts=int(d.get('attempts') or 0),
            next_attempt_at=d.get('next_attempt_at') or d.get('scheduled_for'),
            claim_token=UUID(str(d['claim_token'])) if d.get('claim_token') else None,
            lease_until=d.get('lease_until'),
            dispatch_started_at=d.get('dispatch_started_at'),
            sent_at=d.get('sent_at'),
            superseded_at=d.get('superseded_at'),
            error_code=d.get('error_code'),
            switch_reply_message_id=d.get('switch_reply_message_id'),
            notification_body=d.get('notification_body'),
        )

    async def claim(self, limit):
        if self.client is not None:
            result = await self.call('claim_reminder_deliveries', p_limit=limit)
            try:
                rows = [ReminderDelivery.model_validate(row) for row in result]
                for row in rows:
                    self.validate_scope(row.intention.tenant_id)
                return rows
            except (ValidationError, TypeError):
                raise CommitmentError('invalid_storage_result', 503) from None

        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        lease_iso = (now_dt + timedelta(minutes=2)).isoformat()
        async with service_client('database') as client:
            resp = await client.get(
                _rest_url(self.settings, 'reminder_deliveries'),
                headers=_supabase_headers(self.settings),
                params={
                    'tenant_id': f"eq.{self.settings.switch_tenant_id}",
                    'status': 'in.(pending,failed)',
                    'or': f"(next_attempt_at.lte.{now_iso},scheduled_for.lte.{now_iso})",
                    'attempts': 'lt.3',
                    'occurrence_key': 'not.is.null',
                    'order': 'scheduled_for.asc',
                    'limit': str(limit),
                },
                timeout=10,
            )
            candidates = resp.json() if resp.status_code == 200 else []
            claimed = []
            for d in candidates:
                token = str(uuid4())
                upd = await client.patch(
                    _rest_url(self.settings, 'reminder_deliveries'),
                    headers=_supabase_headers(self.settings, prefer='return=representation'),
                    params={'id': f"eq.{d['id']}"},
                    json={'status': 'sending', 'claim_token': token, 'lease_until': lease_iso},
                    timeout=10,
                )
                if upd.status_code == 200 and upd.json():
                    claimed.append(self._row_to_delivery(upd.json()[0]))
            return claimed

    def reference(self, delivery):
        self.validate_scope(delivery.intention.tenant_id)
        return {'p_delivery_id': str(delivery.id), 'p_claim_token': str(delivery.claim_token)}

    async def context(self, delivery):
        if self.client is not None:
            result = await self.call('read_reminder_dispatch', **self.reference(delivery))
            if result is None:
                return None
            try:
                parsed = DispatchContext.model_validate(result)
                self.validate_scope(parsed.work.commitment.tenant_id, parsed.work.switch_room_id)
                if (
                    parsed.delivery.id != delivery.id
                    or parsed.delivery.claim_token != delivery.claim_token
                    or parsed.delivery.intention != delivery.intention
                    or (
                        parsed.work.commitment.tenant_id,
                        parsed.work.commitment.room_id,
                        parsed.work.commitment.id,
                    )
                    != (
                        delivery.intention.tenant_id,
                        delivery.intention.room_id,
                        delivery.intention.commitment_id,
                    )
                ):
                    raise CommitmentError('invalid_storage_result', 503)
                return parsed
            except ValidationError:
                raise CommitmentError('invalid_storage_result', 503) from None

        async with service_client('database') as client:
            d_resp = await client.get(
                _rest_url(self.settings, 'reminder_deliveries'),
                headers=_supabase_headers(self.settings),
                params={'id': f"eq.{delivery.id}", 'claim_token': f"eq.{delivery.claim_token}"},
                timeout=10,
            )
            d_rows = d_resp.json() if d_resp.status_code == 200 else []
            if not d_rows:
                return None
            d_obj = self._row_to_delivery(d_rows[0])
            c_resp = await client.get(
                _rest_url(self.settings, 'commitments'),
                headers=_supabase_headers(self.settings),
                params={'id': f"eq.{d_obj.intention.commitment_id}"},
                timeout=10,
            )
            c_rows = c_resp.json() if c_resp.status_code == 200 else []
            if not c_rows:
                return None
            r_resp = await client.get(
                _rest_url(self.settings, 'rooms'),
                headers=_supabase_headers(self.settings),
                params={'id': f"eq.{d_obj.intention.room_id}"},
                timeout=10,
            )
            r_rows = r_resp.json() if r_resp.status_code == 200 else []
            if not r_rows:
                return None
            r_plat = r_rows[0].get('platform', 'discord')
            work_obj = await self._build_work_from_rest(client, c_rows[0], r_rows[0]['switch_room_id'], platform=r_plat)
            return DispatchContext(delivery=d_obj, work=work_obj)

    async def begin(self, delivery, body):
        if self.client is not None:
            return await self.call('begin_reminder_dispatch', **self.reference(delivery), p_body=body) is True
        now_iso = datetime.now(timezone.utc).isoformat()
        async with service_client('database') as client:
            upd = await client.patch(
                _rest_url(self.settings, 'reminder_deliveries'),
                headers=_supabase_headers(self.settings, prefer='return=representation'),
                params={'id': f"eq.{delivery.id}", 'claim_token': f"eq.{delivery.claim_token}"},
                json={
                    'dispatch_started_at': now_iso,
                    'attempts': delivery.attempts + 1,
                    'notification_body': body[:2000],
                },
                timeout=10,
            )
            return upd.status_code == 200 and bool(upd.json())

    async def defer(self, delivery, until):
        if self.client is not None:
            return (
                await self.call(
                    'defer_reminder_delivery',
                    **self.reference(delivery),
                    p_until=until.isoformat() if until else None,
                )
                is True
            )
        async with service_client('database') as client:
            upd = await client.patch(
                _rest_url(self.settings, 'reminder_deliveries'),
                headers=_supabase_headers(self.settings, prefer='return=representation'),
                params={'id': f"eq.{delivery.id}", 'claim_token': f"eq.{delivery.claim_token}"},
                json={
                    'status': 'pending' if until else 'superseded',
                    'next_attempt_at': until.isoformat() if until else None,
                    'claim_token': None,
                    'lease_until': None,
                },
                timeout=10,
            )
            return upd.status_code == 200 and bool(upd.json())

    async def finish(self, delivery, outcome, message_id=None, error=None):
        if self.client is not None:
            saved = await self.call(
                'finish_reminder_delivery',
                **self.reference(delivery),
                p_outcome=outcome,
                p_message_id=message_id,
                p_error=error,
            )
            if saved is not True:
                raise CommitmentError('reminder_acknowledgement_not_saved', 503)
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        async with service_client('database') as client:
            if outcome == 'sent':
                payload = {
                    'status': 'sent',
                    'sent_at': now_iso,
                    'switch_reply_message_id': message_id,
                    'next_attempt_at': None,
                    'claim_token': None,
                    'lease_until': None,
                }
            else:
                payload = {
                    'status': 'ambiguous' if outcome == 'ambiguous' else 'failed',
                    'error_code': error or 'send_outcome_unknown',
                    'claim_token': None,
                    'lease_until': None,
                }
            upd = await client.patch(
                _rest_url(self.settings, 'reminder_deliveries'),
                headers=_supabase_headers(self.settings, prefer='return=representation'),
                params={'id': f"eq.{delivery.id}"},
                json=payload,
                timeout=10,
            )
            if upd.status_code != 200 or not upd.json():
                raise CommitmentError('reminder_acknowledgement_not_saved', 503)
