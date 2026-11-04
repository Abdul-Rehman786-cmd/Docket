"""Bounded, process-local viewer delivery. No database or Switch connections."""

import asyncio
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from time import monotonic
from uuid import uuid4

from anyio import CancelScope
from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.core.config import get_settings
from app.core.security import ingest_token_matches, viewer_origin_matches
from app.models.viewer import ViewerAuthentication
from app.services.ai_service import response_scope
from app.services.message_service import get_recent_messages

AUTH_TIMEOUT = 5
SEND_TIMEOUT = 5
HEARTBEAT_INTERVAL = 10
HEARTBEAT_TIMEOUT = 35
MAX_CONNECTIONS = 64
MAX_BUFFERED_EVENTS = 32


class ViewerClosed(Exception):
    def __init__(self, code):
        self.code = code


@dataclass
class Subscription:
    connection_id: str = field(default_factory=lambda: str(uuid4()))
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=MAX_BUFFERED_EVENTS))
    scope: tuple | None = None
    token_digest: bytes = b""
    last_pong: float = field(default_factory=monotonic)


class ViewerHub:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.connections: dict[str, Subscription] = {}
        self.closed = False

    def reserve(self):
        if self.closed or len(self.connections) >= MAX_CONNECTIONS:
            raise ViewerClosed(1013)
        subscription = Subscription()
        self.connections[subscription.connection_id] = subscription
        return subscription

    def remove(self, subscription):
        self.connections.pop(subscription.connection_id, None)

    @staticmethod
    def stop(subscription, code):
        while not subscription.queue.empty():
            subscription.queue.get_nowait()
        subscription.queue.put_nowait(ViewerClosed(code))

    def publish(self, kind, scope, payload):
        # Repositories can also be called by synchronous local utilities/tests.
        # Schedule only the bounded fan-out, never a task per event or connection.
        if not self.closed:
            try:
                if asyncio.get_running_loop() is self.loop:
                    self._publish(kind, scope, payload)
                    return
            except RuntimeError:
                pass
            self.loop.call_soon_threadsafe(self._publish, kind, scope, payload)

    def _publish(self, kind, scope, payload):
        if self.closed:
            return
        for subscription in self.connections.values():
            authorized = subscription.scope
            if authorized is None:
                continue
            if kind.startswith("message_"):
                matches = scope[0] == authorized[0] and scope[1] in authorized[1]
            else:
                matches = scope == authorized
            if matches:
                try:
                    subscription.queue.put_nowait({"type": kind, **payload})
                except asyncio.QueueFull:
                    self.stop(subscription, 1013)

    def close(self):
        self.closed = True
        for subscription in self.connections.values():
            self.stop(subscription, 1012)


def current_settings(subscription):
    settings = get_settings()
    expected = settings.switch_ingest_token.get_secret_value()
    if not expected or not secrets.compare_digest(
        subscription.token_digest, hashlib.sha256(expected.encode("utf-8")).digest()
    ):
        raise ViewerClosed(4401)
    try:
        if response_scope(settings) != subscription.scope:
            raise ViewerClosed(4403)
    except HTTPException:
        raise ViewerClosed(4403) from None
    return settings


def snapshot(subscription, settings, state):
    return {
        "type": "snapshot", "connection_id": subscription.connection_id,
        "messages": [row.model_dump(mode="json") for row in get_recent_messages(settings, state.message_receipts)],
        "answers": [row.model_dump(mode="json") for row in state.ai_service.repository.recent(subscription.scope)],
    }


async def send(websocket, payload):
    await asyncio.wait_for(websocket.send_json(payload), SEND_TIMEOUT)


async def write_updates(websocket, subscription, state):
    next_heartbeat = monotonic() + HEARTBEAT_INTERVAL
    while True:
        try:
            event = await asyncio.wait_for(subscription.queue.get(), max(0, next_heartbeat - monotonic()))
        except TimeoutError:
            event = {"type": "ping"}
        settings = current_settings(subscription)
        if monotonic() - subscription.last_pong > HEARTBEAT_TIMEOUT:
            raise ViewerClosed(4408)
        if isinstance(event, ViewerClosed):
            raise event
        if event["type"] == "resync":
            event = snapshot(subscription, settings, state)
        await send(websocket, event)
        # Busy feeds also need pongs; message traffic must not postpone heartbeats.
        if event["type"] == "ping":
            next_heartbeat = monotonic() + HEARTBEAT_INTERVAL
        elif monotonic() >= next_heartbeat:
            await send(websocket, {"type": "ping"})
            next_heartbeat = monotonic() + HEARTBEAT_INTERVAL


async def read_commands(websocket, subscription):
    while True:
        raw = await websocket.receive_text()
        if len(raw) > 1024:
            raise ViewerClosed(1008)
        try:
            command = json.loads(raw)
        except ValueError:
            raise ViewerClosed(1008) from None
        if command == {"type": "pong"}:
            subscription.last_pong = monotonic()
        elif command == {"type": "resync"}:
            if subscription.queue.empty():
                subscription.queue.put_nowait({"type": "resync"})
        else:
            raise ViewerClosed(1008)


async def serve_viewer(websocket: WebSocket):
    # Tokens are accepted only in the first frame, never as URL parameters.
    if websocket.url.query or not viewer_origin_matches(websocket):
        await websocket.close(code=1008)
        return
    hub = getattr(websocket.app.state, "viewer_hub", None)
    if hub is None:
        await websocket.close(code=1013)
        return
    try:
        subscription = hub.reserve()
    except ViewerClosed:
        await websocket.close(code=1013)
        return
    tasks = []
    close_code = 1000
    try:
        await websocket.accept()
        raw = await asyncio.wait_for(websocket.receive_text(), AUTH_TIMEOUT)
        if len(raw) > 8192:
            raise ViewerClosed(4401)
        try:
            authentication = ViewerAuthentication.model_validate_json(raw)
        except ValidationError:
            raise ViewerClosed(4401) from None
        settings = get_settings()
        token = authentication.token.get_secret_value()
        if not ingest_token_matches(token, settings):
            raise ViewerClosed(4401)
        subscription.token_digest = hashlib.sha256(token.encode("utf-8")).digest()
        del raw, token, authentication
        try:
            subscription.scope = response_scope(settings)
        except HTTPException:
            raise ViewerClosed(4403) from None
        await send(websocket, snapshot(subscription, current_settings(subscription), websocket.app.state))
        tasks = [asyncio.create_task(read_commands(websocket, subscription)),
                 asyncio.create_task(write_updates(websocket, subscription, websocket.app.state))]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except ViewerClosed as error:
        close_code = error.code
    except TimeoutError:
        close_code = 4408
    except (WebSocketDisconnect, OSError):
        pass
    except (ValueError, KeyError):
        # Malformed/binary frames never reflect their contents into diagnostics.
        close_code = 1008
    finally:
        # Remove synchronously before cancellation can interrupt awaited cleanup.
        hub.remove(subscription)
        for task in tasks:
            task.cancel()
        with CancelScope(shield=True):
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.wait_for(websocket.close(code=close_code), SEND_TIMEOUT)
            except (RuntimeError, WebSocketDisconnect, OSError, TimeoutError):
                pass
