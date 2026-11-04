"""Small server-side Switch transport. Errors never contain remote payloads."""

import asyncio
import json
from contextlib import asynccontextmanager
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect

from app.services.switch_listener import frame_parts, socket_logger


class SwitchError(Exception):
    pass


class SwitchAPI:
    def __init__(self, base_url: str, agent_id: str, api_key: str):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username
                or parsed.password or parsed.query or parsed.fragment or not agent_id or not api_key):
            raise SwitchError("Switch client is not configured correctly.")
        self.base_url = base_url.rstrip("/")
        self.agent_path = "/agents/" + quote(agent_id, safe="")
        self.headers = {"Authorization": "Bearer " + api_key}

    @asynccontextmanager
    async def http(self):
        async with httpx.AsyncClient(
            base_url=self.base_url + "/", headers=self.headers,
            timeout=20, trust_env=False, follow_redirects=False,
        ) as client:
            yield client

    async def operation(self, client, name: str, args: dict, connection_id: str | None = None):
        headers = {"X-Switch-Connection-Id": connection_id} if connection_id else {}
        response = await client.post(self.agent_path.lstrip("/") + "/ops/" + name, json=args, headers=headers)
        if response.status_code != 200:
            raise SwitchError("Switch rejected the operation. Check agent access and runtime availability.")
        try:
            return response.json()["result"]
        except (ValueError, KeyError, TypeError):
            raise SwitchError("Switch returned an invalid operation response.") from None

    @asynccontextmanager
    async def room_connection(self, room_id: str):
        """Claim only the requester's internal room; never the AI's connection."""
        connection_id = str(uuid4())
        parsed = urlsplit(self.base_url)
        url = urlunsplit((
            "wss" if parsed.scheme == "https" else "ws", parsed.netloc,
            parsed.path + self.agent_path + "/connection/ws",
            urlencode({"connection_id": connection_id, "rooms": room_id,
                       "scope": "single", "filter": "all", "start_from": "head"}), "",
        ))
        async with connect(url, additional_headers=self.headers, proxy=None, ping_interval=None,
                           open_timeout=15, close_timeout=3, max_size=1048576, logger=socket_logger) as socket:
            queue = asyncio.Queue(maxsize=256)
            ready = asyncio.get_running_loop().create_future()

            async def read():
                cursor = 0
                try:
                    async for raw in socket:
                        frame = json.loads(raw)
                        kind, data = frame_parts(frame)
                        if kind == "ping":
                            await socket.send(json.dumps({"type": "pong", "cursor": cursor}))
                        elif kind == "connection_state":
                            if room_id not in data.get("rooms", []) or type(data.get("cursor")) is not int:
                                raise SwitchError("Switch did not cover the internal response room.")
                            cursor = data["cursor"]
                            if not ready.done():
                                ready.set_result(None)
                        elif kind in ("refused", "evicted", "gap", "room_released"):
                            raise SwitchError("Switch interrupted the response connection. Request was not retried.")
                        elif kind in ("message", "command", "room_join"):
                            sequence = frame.get("id")
                            if type(sequence) is not int or sequence != data.get("sequence"):
                                raise SwitchError("Switch returned an invalid event sequence.")
                            if sequence <= cursor:
                                continue
                            queue.put_nowait((kind, data))
                            cursor = sequence
                    raise SwitchError("Switch closed the response connection.")
                except Exception:
                    error = SwitchError("Switch response connection failed. Request was not retried.")
                    if not ready.done():
                        ready.set_exception(error)
                    # Always wake a caller that is waiting for a reply.
                    if queue.full():
                        queue.get_nowait()
                    queue.put_nowait(error)

            reader = asyncio.create_task(read())
            try:
                await asyncio.wait_for(ready, timeout=20)
                yield connection_id, queue
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)


def require_internal_room(detail: dict, room_id: str, target_name: str) -> None:
    # Missing routing fields are unsafe, rather than equivalent to null.
    if (not isinstance(detail, dict) or detail.get("id") != room_id
            or "bridge_id" not in detail or detail["bridge_id"] is not None
            or "external_channel_id" not in detail or detail["external_channel_id"] is not None
            or detail.get("archived", False) or target_name not in detail.get("agent_names", [])):
        raise SwitchError("AI responses require an internal room with no messaging bridge and the configured AI agent.")


def require_observer_only_input(detail: dict, room_id: str, target_name: str, requester_name: str) -> None:
    if (not isinstance(detail, dict) or detail.get("id") != room_id
            or not isinstance(detail.get("agent_names"), list)
            or requester_name not in detail["agent_names"] or target_name in detail["agent_names"]):
        raise SwitchError("Input rooms must contain the listener without Codex. Rerun Switch setup to keep answers out of Discord.")


def require_no_input_sessions(agent_detail: dict, input_rooms: frozenset[str]) -> None:
    sessions = agent_detail.get("sessions") if isinstance(agent_detail, dict) else None
    if not isinstance(sessions, list) or any(not isinstance(session, dict) for session in sessions):
        raise SwitchError("Could not verify Codex's active room sessions. No question was posted.")
    ended = {"stopped", "closed", "ended", "expired", "disconnected"}
    if any(session.get("room_id") in input_rooms and session.get("state") not in ended for session in sessions):
        raise SwitchError("Codex still has an input-room session. Stop that Discord session in Switch Console, keep the internal response session running, then rerun Switch setup.")
