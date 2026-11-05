"""Pinned vendor media contract; only configured Switch receives credentials.

Verified public source e84534af637e56c6a0a66c8a0b900200a70c7f5f,
core/switch_core/bridges/agent/api/handlers.py:509. MXC is passed as an opaque
query value to Switch, never resolved/fetched as a network origin here.
The native PostgreSQL transport issues switch-media:// plus uuid4().hex
(core/switch_core/transport/postgres.py); mxc is the legacy field name.
"""

import asyncio
import logging
import re
from urllib.parse import quote

import httpx

from app.services.switch_api import SwitchAPI
from app.core.config import BACKEND_DIR
from app.repositories.listener_state import ListenerState, listener_identity, document_reply_session


class AttachmentFailure(Exception):
    def __init__(self,code,retryable=False):
        self.code,self.retryable=code,retryable
        super().__init__(code)


def valid_media_reference(value):
    return isinstance(value,str) and bool(re.fullmatch(
        r"(?:switch-media://[0-9a-f]{32}|mxc://[A-Za-z0-9.:-]{1,253}/[A-Za-z0-9_-]{1,512})",value))


class SwitchDocumentTransport:
    def __init__(self,settings):
        self.settings=settings
        self.api=SwitchAPI(settings.switch_bridge_url,settings.listener_agent_id,settings.listener_api_key.get_secret_value())

    async def download(self,room,attachment):
        if not valid_media_reference(attachment["mxc"]):
            raise AttachmentFailure("unsafe_media_reference")
        limit=self.settings.switch_document_max_bytes
        url=self.api.base_url+self.api.agent_path+"/rooms/"+quote(room,safe="")+"/media"
        try:
            async with asyncio.timeout(self.settings.switch_document_download_seconds), self.api.http() as client:
                async with client.stream("GET",url,params={"mxc":attachment["mxc"]},headers=self.api.headers,
                    follow_redirects=False,timeout=httpx.Timeout(self.settings.switch_document_download_seconds,
                                                                connect=min(5,self.settings.switch_document_download_seconds))) as response:
                    logging.getLogger("team_memory").info("document stage=download http_status=%d",response.status_code)
                    if response.status_code!=200:
                        if response.status_code in (401,403):
                            raise AttachmentFailure("media_access_denied")
                        raise AttachmentFailure("media_unavailable",response.status_code==429 or response.status_code>=500)
                    declared=response.headers.get("content-length")
                    if declared and (not declared.isdecimal() or int(declared)>limit):
                        raise AttachmentFailure("attachment_too_large")
                    raw=bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw)+len(chunk)>limit:
                            raise AttachmentFailure("attachment_too_large")
                        raw.extend(chunk)
                    logging.getLogger("team_memory").info("document stage=download_complete bytes=%d",len(raw))
                    return bytes(raw)
        except (httpx.HTTPError,TimeoutError):
            raise AttachmentFailure("media_unavailable",True) from None

    async def post(self,room,body,thread):
        # One operation per attempt; no alternate-route retry after an uncertain send.
        async with asyncio.timeout(20), self.api.http() as client:
            state_path=BACKEND_DIR/".local"/"switch-listener.json"
            if room not in self.settings.allowed_switch_rooms or not state_path.exists():
                raise AttachmentFailure("reply_listener_unavailable")
            state=ListenerState.load(state_path,listener_identity(self.settings))
            if state.generation is None:
                raise AttachmentFailure("reply_listener_unavailable")
            args={"body":body}
            if thread:
                args["thread_id"]=thread
            await self.api.operation(client,"post_message",args,connection_id=state.connection_id,
                                     session_id=document_reply_session(room))
