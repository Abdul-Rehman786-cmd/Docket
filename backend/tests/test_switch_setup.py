import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from app import switch_setup
from app.core.config import Settings
from app.services.switch_api import SwitchAPI, SwitchError, require_no_input_sessions


def configure(monkeypatch, settings, registration_status=201, owner="fixture-owner", private_has_requester=True):
    calls = []
    input_agents = {room_id: ["codex-discord", "unrelated-agent"] for room_id in settings.allowed_switch_rooms}
    private_agents = ["codex-discord"] + (["team-memory-client"] if private_has_requester else [])

    async def handle(request):
        name = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        calls.append((name, body))
        if name == "get_agent_detail":
            requester = body["agent_id"] == "fixture-requester"
            result = {"name": "team-memory-client" if requester else "codex-discord",
                      "owner_id": owner if requester else "fixture-owner", "sessions": []}
        elif name == "get_room_detail":
            room_id = body["room_id"]
            result = {"id": room_id, "bridge_id": None if room_id == "private-room" else "fixture-bridge",
                      "external_channel_id": None if room_id == "private-room" else "fixture-channel",
                      "agent_names": list(private_agents) if room_id == "private-room" else list(input_agents[room_id])}
        elif name == "list_rooms":
            result = [{"room_id": room_id} for room_id, agents in input_agents.items() if "team-memory-client" in agents]
            if "team-memory-client" in private_agents:
                result.append({"room_id": "private-room"})
        elif name == "invite_agent_to_room":
            agents = private_agents if body["room_id"] == "private-room" else input_agents[body["room_id"]]
            agents.append(body["agent_name"])
            result = {}
        elif name == "remove_agent_from_room":
            assert body["room_id"] in settings.allowed_switch_rooms
            assert body["agent_name"] == "codex-discord"
            input_agents[body["room_id"]].remove(body["agent_name"])
            result = {}
        elif name == "agents":
            return httpx.Response(registration_status, json={})
        else:
            raise AssertionError("Unexpected remote mutation")
        return httpx.Response(200, json={"result": result})

    @asynccontextmanager
    async def fake_http(self):
        async with httpx.AsyncClient(base_url="http://fixture/", transport=httpx.MockTransport(handle), headers=self.headers) as client:
            yield client

    monkeypatch.setattr(SwitchAPI, "http", fake_http)
    monkeypatch.setattr(switch_setup, "get_settings", lambda: settings)
    monkeypatch.setattr(switch_setup, "save_settings", lambda values: None)
    return calls


def test_setup_reuses_saved_identity_and_room_without_registration_or_creation(monkeypatch):
    settings = Settings(_env_file=None, switch_requester_agent_id="fixture-requester",
                        switch_requester_api_key="fixture-key", switch_ai_room_id="private-room",
                        switch_ai_sender_id="fixture-sender")
    calls = configure(monkeypatch, settings)
    asyncio.run(switch_setup.setup())
    assert "agents" not in [name for name, _ in calls]
    assert "create_room" not in [name for name, _ in calls]
    assert sum(name == "invite_agent_to_room" for name, _ in calls) == 2
    assert sum(name == "remove_agent_from_room" for name, _ in calls) == 2


def test_restored_client_joins_existing_private_room_without_recreating_it(monkeypatch):
    settings = Settings(_env_file=None, switch_requester_agent_id="fixture-requester",
                        switch_requester_api_key="fixture-key", switch_ai_room_id="private-room",
                        switch_ai_sender_id="fixture-sender")
    calls = configure(monkeypatch, settings, private_has_requester=False)
    asyncio.run(switch_setup.setup())
    invites = [body for name, body in calls if name == "invite_agent_to_room" and body["room_id"] == "private-room"]
    assert invites == [{"room_id": "private-room", "agent_name": "team-memory-client"}]
    assert not any(name in ("agents", "create_room") for name, _ in calls)


def test_input_room_change_is_idempotent_and_preserves_other_agents_and_transport(monkeypatch):
    settings = Settings(_env_file=None, switch_requester_agent_id="fixture-requester",
                        switch_requester_api_key="fixture-key", switch_ai_room_id="private-room")
    calls = configure(monkeypatch, settings)
    api = SwitchAPI(settings.switch_bridge_url, settings.switch_requester_agent_id, "fixture-key")

    async def run():
        await switch_setup.configure_input_rooms(api, settings)
        first_count = len(calls)
        await switch_setup.configure_input_rooms(api, settings)
        assert all(name == "get_room_detail" for name, _ in calls[first_count:])
        async with api.http() as client:
            for room_id in settings.allowed_switch_rooms:
                room = await api.operation(client, "get_room_detail", {"room_id": room_id})
                assert set(room["agent_names"]) == {"unrelated-agent", "team-memory-client"}
                assert room["bridge_id"] == "fixture-bridge"
                assert room["external_channel_id"] == "fixture-channel"

    asyncio.run(run())


def test_setup_never_removes_codex_from_response_room(monkeypatch):
    settings = Settings(_env_file=None, switch_ai_room_id="test-room")
    calls = configure(monkeypatch, settings)
    api = SwitchAPI(settings.switch_bridge_url, settings.switch_agent_id, "fixture-key")
    with pytest.raises(SwitchError, match="separate"):
        asyncio.run(switch_setup.configure_input_rooms(api, settings))
    assert calls == []


def test_registration_conflict_never_replaces_existing_agent(monkeypatch):
    settings = Settings(_env_file=None, switch_registration_token="fixture-registration-key")
    calls = configure(monkeypatch, settings, registration_status=409)
    with pytest.raises(SwitchError, match="already exists"):
        asyncio.run(switch_setup.setup())
    registration = next(body for name, body in calls if name == "agents")
    assert "overwrite" not in registration
    assert registration["models"] == []
    assert not any(name in ("create_room", "invite_agent_to_room") for name, _ in calls)


def test_wrong_registration_owner_does_not_widen_permissions(monkeypatch):
    settings = Settings(_env_file=None, switch_requester_agent_id="fixture-requester", switch_requester_api_key="fixture-key")
    calls = configure(monkeypatch, settings, owner="another-owner")
    with pytest.raises(SwitchError, match="same Switch owner"):
        asyncio.run(switch_setup.setup())
    assert [name for name, _ in calls] == ["get_agent_detail", "get_agent_detail"]


def test_live_input_session_blocks_success_even_if_room_membership_was_removed():
    with pytest.raises(SwitchError, match="Stop that Discord session"):
        require_no_input_sessions({"sessions": [{"room_id": "test-room", "state": "live"}]}, frozenset({"test-room"}))


def test_private_live_session_and_ended_input_session_are_allowed():
    require_no_input_sessions({"sessions": [
        {"room_id": "private-room", "state": "live"},
        {"room_id": "test-room", "state": "stopped"},
    ]}, frozenset({"test-room"}))


@pytest.mark.parametrize("detail", [{}, {"sessions": None}, {"sessions": [None]}])
def test_missing_runtime_evidence_never_claims_server_only_success(detail):
    with pytest.raises(SwitchError, match="Could not verify"):
        require_no_input_sessions(detail, frozenset({"test-room"}))
