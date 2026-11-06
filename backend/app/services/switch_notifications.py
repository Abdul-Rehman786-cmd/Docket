"""Reusable Switch notification sink. One request; uncertain outcomes are quarantined."""
import httpx
from app.models.reminders import NotificationFailure,NotificationReceipt
from app.services.switch_api import SwitchAPI,SwitchError
from app.core.message_logging import redact_source_identifier


class SwitchNotificationSink:
    def __init__(self,settings):self.settings=settings

    async def send(self,room_id,body,thread_id=None):
        s=self.settings
        if (not s.reminder_delivery_enabled or room_id not in s.allowed_switch_rooms or room_id==s.switch_ai_room_id
            or not s.switch_requester_agent_id or s.switch_requester_agent_id==s.switch_agent_id):
            raise NotificationFailure('permanent','delivery_not_configured')
        try:api=SwitchAPI(s.switch_bridge_url,s.switch_requester_agent_id,s.switch_requester_api_key.get_secret_value())
        except SwitchError:raise NotificationFailure('permanent','delivery_not_configured') from None
        payload={'room_id':room_id,'body':body}
        if thread_id:payload['thread_id']=thread_id
        try:
            async with api.http() as client:
                response=await client.post(api.base_url+api.agent_path+'/ops/post_message',headers=api.headers,json=payload)
                if response.status_code == 400 and 'room_id' in response.text:
                    direct_payload = {'room_id': room_id, 'content': body, 'body': body}
                    if thread_id: direct_payload['thread_id'] = thread_id
                    response = await client.post(api.base_url + api.agent_path + '/message', headers=api.headers, json=direct_payload)
        except (httpx.ConnectError,httpx.ConnectTimeout,httpx.PoolTimeout):
            raise NotificationFailure('transient','switch_connect_failed') from None
        except httpx.HTTPError:
            raise NotificationFailure('ambiguous','send_outcome_unknown') from None
        if response.status_code==429:raise NotificationFailure('transient','switch_rate_limited')
        if 400<=response.status_code<500:raise NotificationFailure('permanent','switch_rejected')
        if response.status_code not in (200, 201):raise NotificationFailure('ambiguous','send_outcome_unknown')
        try:
            data = response.json()
            result = data.get('result') if isinstance(data, dict) and isinstance(data.get('result'), dict) else data
            reference = result.get('event_id') or result.get('message_id')
            if not isinstance(reference,str) or not reference or redact_source_identifier(reference,s)!=reference:raise ValueError()
            return NotificationReceipt(message_id=reference)
        except (ValueError,TypeError,KeyError,AttributeError):
            raise NotificationFailure('ambiguous','acknowledgement_unavailable') from None
