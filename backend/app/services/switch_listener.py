import asyncio
import hashlib
import json
import logging
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import httpx
from pydantic import ValidationError
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from app.core.config import BACKEND_DIR, Settings
from app.models.messages import IncomingMessage, MessageReceipt, MessageStatus
from app.core.feature_logging import feature_event
from app.repositories.listener_state import ListenerState
from app.services.message_service import message_reference

logger = logging.getLogger(__name__)
# WebSocket debug traces include Authorization headers. Never enable them on this socket.
socket_logger = logging.Logger("team_memory.switch_socket", level=logging.CRITICAL + 1)
socket_logger.propagate = False


class ListenerStopped(Exception):
    """Safe, fixed diagnostic text; never attach remote payloads or exceptions."""


class RetryConnection(Exception):
    pass


def validate_settings(settings: Settings) -> None:
    required = {
        "SWITCH_BRIDGE_URL": settings.switch_bridge_url,
        "Switch listener agent ID": settings.listener_agent_id,
        "Switch listener API key": settings.listener_api_key.get_secret_value(),
        "SWITCH_INGEST_TOKEN": settings.switch_ingest_token.get_secret_value(),
        "SWITCH_TENANT_ID": settings.switch_tenant_id,
        "SWITCH_ROOM_IDS": settings.allowed_switch_rooms,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ListenerStopped("Missing configuration: " + ", ".join(missing))
    for value in (settings.switch_bridge_url, settings.switch_backend_url):
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https") or not parsed.netloc
            or parsed.username or parsed.password or parsed.query or parsed.fragment
        ):
            raise ListenerStopped("Bridge and backend URLs must be HTTP(S) base URLs without credentials or queries.")


def frame_parts(frame: dict) -> tuple[str, dict]:
    if not isinstance(frame, dict):
        raise ListenerStopped("Switch returned an invalid frame.")
    kind = frame.get("event") or frame.get("type")
    data = frame.get("data", frame)
    if not isinstance(kind, str) or not isinstance(data, dict):
        raise ListenerStopped("Switch returned an invalid frame.")
    return kind, data


class SwitchListener:
    def __init__(self, settings: Settings, state_path: Path | None = None):
        validate_settings(settings)
        self.settings = settings
        self.state_path = state_path or BACKEND_DIR / ".local" / "switch-listener.json"
        identity = json.dumps([
            settings.switch_bridge_url, settings.listener_agent_id,
            str(settings.switch_tenant_id), sorted(settings.allowed_switch_rooms),
        ])
        identity_ref = hashlib.sha256(identity.encode()).hexdigest()
        self.state = ListenerState.load(self.state_path, identity_ref)
        self.registered_agent_name = None

    def socket_url(self, state: ListenerState | None = None, *, claim_rooms: bool = True) -> str:
        state = state or self.state
        parsed = urlsplit(self.settings.switch_bridge_url)
        params = {
            "connection_id": state.connection_id, "scope": "all", "filter": "all",
            "start_from": str(state.cursor) if state.cursor is not None else "head",
        }
        if state.generation is not None:
            params["expected_generation"] = str(state.generation)
        if claim_rooms:
            # Declare at open: subscribing after catch-up can skip buffered events.
            params["rooms"] = ",".join(sorted(self.settings.allowed_switch_rooms))
        path = parsed.path.rstrip("/") + "/agents/" + quote(self.settings.listener_agent_id, safe="") + "/connection/ws"
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunsplit((scheme, parsed.netloc, path, urlencode(params), ""))

    def _connect(self, state: ListenerState | None = None, *, claim_rooms: bool = True):
        return connect(
            self.socket_url(state, claim_rooms=claim_rooms),
            additional_headers={"Authorization": "Bearer " + self.settings.listener_api_key.get_secret_value()},
            proxy=None, ping_interval=None, open_timeout=15, close_timeout=3,
            max_size=1048576, logger=socket_logger,
        )

    def http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.settings.switch_backend_url.rstrip("/") + "/",
            headers={"X-Ingest-Token": self.settings.switch_ingest_token.get_secret_value(),
                     "X-Adapter-Token": self.settings.switch_adapter_token.get_secret_value()},
            timeout=10, trust_env=False, follow_redirects=False,
        )

    async def delivery_status(self, client: httpx.AsyncClient) -> MessageStatus:
        response = await client.get("messages/status")
        if response.status_code != 200:
            raise ListenerStopped("API status check failed. Check the API process and SWITCH_INGEST_TOKEN.")
        try:
            return MessageStatus.model_validate(response.json())
        except (ValueError, ValidationError):
            raise ListenerStopped("The backend returned an invalid status response.") from None

    async def check_connection(self) -> int:
        # A separate unclaimed connection doesn't move the running listener's saved cursor.
        state = ListenerState(identity=self.state.identity, connection_id=str(uuid4()))
        async with self._connect(state, claim_rooms=False) as socket:
            async with asyncio.timeout(15):
                while True:
                    frame = json.loads(await socket.recv())
                    kind, data = frame_parts(frame)
                    if kind == "connection_state":
                        break
                    if kind in ("refused", "evicted"):
                        raise ListenerStopped("Switch refused the listener. Check agent credentials and whether it is controller-managed.")
        # Room membership is distinct from this temporary connection's room claims.
        async with httpx.AsyncClient(
            timeout=10, trust_env=False, follow_redirects=False,
            headers={"Authorization": "Bearer " + self.settings.listener_api_key.get_secret_value()},
        ) as client:
            url = self.settings.switch_bridge_url.rstrip("/") + "/agents/" + quote(self.settings.listener_agent_id, safe="") + "/ops/list_rooms"
            response = await client.post(url, json={})
        if response.status_code != 200:
            raise ListenerStopped("Switch room membership check failed.")
        try:
            rooms = response.json()["result"]
            assigned = {room["room_id"] for room in rooms if not room.get("archived")}
        except (ValueError, TypeError, KeyError):
            raise ListenerStopped("Switch returned an invalid room membership response.") from None
        if not self.settings.allowed_switch_rooms.issubset(assigned):
            raise ListenerStopped("A configured room is not assigned to this agent. Check the room allow-list and invite the agent if needed.")
        return len(self.settings.allowed_switch_rooms)

    async def _read_frames(self, socket, queue: asyncio.Queue) -> None:
        async for raw in socket:
            try:
                frame = json.loads(raw)
            except (TypeError, ValueError):
                raise ListenerStopped("Switch sent invalid JSON; listener stopped without acknowledging it.") from None
            kind, data = frame_parts(frame)
            if kind == "ping":
                await socket.send(json.dumps({"type": "pong", "cursor": self.state.cursor or 0}))
            elif kind == "connection_state":
                generation, cursor = data.get("generation"), data.get("cursor")
                if type(generation) is not int or type(cursor) is not int:
                    raise ListenerStopped("Switch sent an invalid connection state.")
                self.state.generation = generation
                if self.state.cursor is None:
                    self.state.cursor = cursor
                self.state.save(self.state_path)
                covered = self.settings.allowed_switch_rooms.intersection(data.get("rooms", []))
                logger.info("Switch connected; configured rooms covered=%d", len(covered))
                feature_event('switch_listener','connected',rooms=len(covered))
                if not covered:
                    logger.warning("No configured room is covered; check agent membership and other clients.")
            elif kind == "gap":
                raise ListenerStopped("Switch reported a retention gap. Listener stopped; review room history before resetting the local cursor.")
            elif kind == "refused":
                raise ListenerStopped("Switch refused the connection. Check credentials, agent type, and competing connections.")
            elif kind == "evicted":
                if data.get("code") in ("heartbeat_lapsed", "no_stream", "unfenced", "connection_error"):
                    raise RetryConnection()
                raise ListenerStopped("Switch evicted this connection. Listener stopped to avoid taking over another client.")
            elif kind in ("message", "command", "room_join"):
                sequence = frame.get("id")
                if type(sequence) is not int or sequence < 0 or data.get("sequence") != sequence:
                    raise ListenerStopped("Switch sent an invalid event sequence.")
                try:
                    queue.put_nowait((kind, data, sequence))
                except asyncio.QueueFull:
                    # Reconnect from the acknowledged cursor rather than block heartbeat handling.
                    raise RetryConnection() from None
            elif kind == "subscription_changed":
                covered = self.settings.allowed_switch_rooms.intersection(data.get("rooms", []))
                logger.info("Switch room coverage changed; configured rooms covered=%d", len(covered))
        raise RetryConnection()

    async def _forward_events(self, queue: asyncio.Queue, client: httpx.AsyncClient) -> None:
        while True:
            kind, data, sequence = await queue.get()
            if self.state.cursor is not None and sequence <= self.state.cursor:
                continue
            if kind == "message" and data.get("room_id") in self.settings.allowed_switch_rooms:
                payload = data.get("payload")
                if not isinstance(payload, dict):
                    raise ListenerStopped("Switch sent an invalid message payload.")
                targets=payload.get('mentioned_agent_ids')
                if targets is not None and (not isinstance(targets,list) or len(targets)>20 or
                    any(not isinstance(target,str) or not target or len(target)>512 or any(c.isspace() for c in target) for target in targets)):
                    targets=[]  # Unsupported metadata denies tasks; preserve ordinary ingestion.
                bot_flag=payload.get('sender_is_bot')
                try:
                    message = IncomingMessage(
                        tenant_id=self.settings.switch_tenant_id, room_id=data["room_id"],
                        message_id=payload.get("message_id"), sender=payload.get("sender"),
                        sender_name=payload.get("sender_name"),
                        body=payload.get("body", ""), addressed=payload.get("addressed", False),
                        timestamp_ms=payload.get("timestamp"), thread_id=payload.get("thread_id"),
                        workspace_id=payload.get("workspace_id"), channel_id=payload.get("channel_id"),
                        external_message_id=payload.get("external_message_id"),
                        external_author_id=payload.get("external_author_id"),
                        registered_agent_id=self.settings.listener_agent_id,
                        registered_agent_name=self.registered_agent_name,
                        mentioned_agent_ids=targets,
                        sender_is_bot=bot_flag if type(bot_flag) is bool else None,
                    )
                except ValidationError:
                    raise ListenerStopped("Switch sent an invalid message; cursor was not advanced.") from None
                try:
                    response = await client.post("messages", json=message.model_dump(mode="json"))
                except httpx.HTTPError:
                    raise RetryConnection() from None
                if response.status_code >= 500 or response.status_code == 429:
                    raise RetryConnection()
                if response.status_code != 200:
                    raise ListenerStopped("API rejected a message. Check the ingest credential, tenant, room allow-list, and payload schema.")
                try:
                    receipt = MessageReceipt.model_validate(response.json())
                except (ValueError, ValidationError):
                    raise ListenerStopped("API returned an invalid receipt; cursor was not advanced.") from None
                if receipt.message_ref != message_reference(message):
                    raise ListenerStopped("API receipt does not match the message; cursor was not advanced.")
                logger.info("API acknowledged message ref=%s duplicate=%s", receipt.message_ref, receipt.duplicate)
                feature_event('switch_listener','duplicate' if receipt.duplicate else 'acknowledged',reference=receipt.message_ref)
            self.state.cursor = sequence
            self.state.save(self.state_path)

    async def _session(self, client: httpx.AsyncClient) -> None:
        async with self._connect() as socket:
            queue: asyncio.Queue = asyncio.Queue(maxsize=256)
            reader = asyncio.create_task(self._read_frames(socket, queue))
            worker = asyncio.create_task(self._forward_events(queue, client))
            try:
                done, _ = await asyncio.wait((reader, worker), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                reader.cancel()
                worker.cancel()
                await asyncio.gather(reader, worker, return_exceptions=True)

    async def run(self) -> None:
        delay = 1
        async with self.http_client() as client:
            # Fail early for a wrong backend URL or credential; no message is consumed.
            await self.delivery_status(client)
            # Only the separately enabled advanced action policy needs this.
            # Simple creation uses Switch's addressed metadata, with no lookup.
            if self.settings.automatic_commitments_enabled:
                from app.services.switch_api import SwitchAPI, SwitchError
                api=SwitchAPI(self.settings.switch_bridge_url,self.settings.listener_agent_id,
                              self.settings.listener_api_key.get_secret_value())
                try:
                    async with api.http() as switch_client:
                        detail=await api.operation(switch_client,'get_agent_detail',{'agent_id':self.settings.listener_agent_id})
                        name=detail.get('name') if isinstance(detail,dict) else None
                        self.registered_agent_name=name if isinstance(name,str) else None
                except (SwitchError,httpx.HTTPError):
                    self.registered_agent_name=None
                    logger.warning('Agent registration unavailable; advanced lifecycle actions will fail closed')
            while True:
                try:
                    await self._session(client)
                except ConnectionClosed as exc:
                    code = exc.rcvd.code if exc.rcvd else None
                    if code in (4400, 4401, 4403, 4409):
                        raise ListenerStopped("Switch rejected the connection. Check the agent key, permissions, and whether another client manages it.") from None
                except InvalidStatus as exc:
                    if exc.response.status_code in (400, 401, 403, 404, 409):
                        raise ListenerStopped("Switch refused the WebSocket handshake. Check the bridge URL and agent configuration.") from None
                except (RetryConnection, OSError, TimeoutError, WebSocketException):
                    pass
                logger.warning("Switch delivery interrupted; reconnecting from acknowledged cursor in %ds", delay)
                feature_event('switch_listener','retry_scheduled',reason='switch_unavailable',wait_seconds=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
