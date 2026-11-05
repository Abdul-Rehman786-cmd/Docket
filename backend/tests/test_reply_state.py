import pytest
from pathlib import Path
from uuid import uuid4

from app.core.config import Settings
from app.models.ai import AskRequest
from app.models.messages import IncomingMessage
from app.repositories.message_receipts import MessageReceiptRepository
from app.repositories.reply_state import ReplyState
from app.services import ai_service
from app.services.ai_service import AIService


def test_reply_state_file_lifecycle(tmp_path: Path):
    state_file = tmp_path / "last_replied_message.txt"
    # When file does not exist, default values are returned
    state = ReplyState.load(state_file)
    assert state.timestamp_ms is None
    assert state.message_id is None

    # Saving state writes to file properly
    state.timestamp_ms = 1730000000000
    state.message_id = "msg-123"
    state.room_id = "test-room"
    state.save(state_file)

    assert state_file.exists()
    loaded = ReplyState.load(state_file)
    assert loaded.timestamp_ms == 1730000000000
    assert loaded.message_id == "msg-123"
    assert loaded.room_id == "test-room"


def test_reply_state_handles_corrupt_file(tmp_path: Path):
    state_file = tmp_path / "last_replied_message.txt"
    state_file.write_text("random garbage @@##", encoding="utf-8")
    loaded = ReplyState.load(state_file)
    assert loaded.timestamp_ms is None


def test_ai_service_watermark_comparison(tmp_path: Path):
    state_file = tmp_path / "last_replied_message.txt"
    service = AIService(reply_state_path=state_file)

    # When no watermark is set, nothing is considered older
    assert not service.is_message_older_than_last_reply(1730000000000)

    # Record reply for a message at timestamp 1000
    service.record_reply(timestamp_ms=1000, message_id="msg-1", room_id="room-a")
    assert service.reply_state.timestamp_ms == 1000

    # Earlier and equal timestamps are older
    assert service.is_message_older_than_last_reply(500)
    assert service.is_message_older_than_last_reply(1000)

    # Later timestamps are NOT older
    assert not service.is_message_older_than_last_reply(1001)
    assert not service.is_message_older_than_last_reply(2000)

    # Recording an older timestamp does not regress watermark
    service.record_reply(timestamp_ms=800, message_id="old", room_id="room-a")
    assert service.reply_state.timestamp_ms == 1000

    # Recording a newer timestamp advances watermark and writes to file
    service.record_reply(timestamp_ms=2500, message_id="msg-2", room_id="room-a")
    assert service.reply_state.timestamp_ms == 2500

    # Reloading from disk preserves the watermark
    reloaded_service = AIService(reply_state_path=state_file)
    assert reloaded_service.reply_state.timestamp_ms == 2500


@pytest.mark.anyio
async def test_ai_service_skips_discord_reply_for_older_message(monkeypatch, tmp_path: Path):
    state_file = tmp_path / "last_replied_message.txt"
    # Seed the watermark at timestamp 2000
    ReplyState(timestamp_ms=2000).save(state_file)

    posted_replies = []

    async def mock_post_room_message(self, room_id, body, thread_id=None):
        posted_replies.append({"room_id": room_id, "body": body})
        return {"event_id": "test-reply"}

    monkeypatch.setattr(ai_service.SwitchAPI, "post_room_message", mock_post_room_message)

    # LLM decision returns query_only
    decision_json = '{"action": "query_only", "search_query": "sprint", "commitments": [], "decisions": [], "memories": [], "answer": ""}'
    monkeypatch.setattr(ai_service, "ask_switch", lambda *args, **kwargs: decision_json)

    service = AIService(reply_state_path=state_file)
    settings = Settings(
        switch_requester_agent_id="test-requester",
        switch_requester_api_key="test-key",
        switch_ai_room_id="ai-room",
        switch_ai_sender_id="ai-sender",
        switch_agent_id="test-agent",
        switch_ai_agent_name="codex-discord",
        switch_tenant_id="00000000-0000-0000-0000-000000000001",
        switch_room_ids="test-room",
        switch_reply_to_discord=True,
        _env_file=None,
    )
    receipts = MessageReceiptRepository()

    # 1. Incoming message timestamp is 1500 (older than watermark 2000)
    old_message = IncomingMessage(
        tenant_id=settings.switch_tenant_id,
        room_id="test-room",
        message_id="msg-old-1500",
        sender="test-user",
        sender_name="User",
        body="What was decided yesterday?",
        addressed=True,
        timestamp_ms=1500,
    )

    await service.ingest(old_message, settings, receipts)
    await service.queue.join()
    await service.close()

    # Nothing should be posted to Discord for the old message!
    assert len(posted_replies) == 0


@pytest.mark.anyio
async def test_ai_service_replies_and_updates_watermark_for_newer_message(monkeypatch, tmp_path: Path):
    state_file = tmp_path / "last_replied_message.txt"
    ReplyState(timestamp_ms=2000).save(state_file)

    posted_replies = []

    async def mock_post_room_message(self, room_id, body, thread_id=None):
        posted_replies.append({"room_id": room_id, "body": body})
        return {"event_id": "test-reply"}

    monkeypatch.setattr(ai_service.SwitchAPI, "post_room_message", mock_post_room_message)
    async def mock_query_team_memory(settings, search_query=None, **kwargs):
        return {"memories": []}

    monkeypatch.setattr(ai_service, "query_team_memory", mock_query_team_memory)

    # LLM decision returns query_only, then context synthesis returns plain answer
    call_count = 0

    async def mock_ask_switch(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return '{"action": "query_only", "search_query": "sprint", "commitments": [], "decisions": [], "memories": [], "answer": ""}'
        return "Sprint ends on Friday."

    monkeypatch.setattr(ai_service, "ask_switch", mock_ask_switch)

    service = AIService(reply_state_path=state_file)
    settings = Settings(
        switch_requester_agent_id="test-requester",
        switch_requester_api_key="test-key",
        switch_ai_room_id="ai-room",
        switch_ai_sender_id="ai-sender",
        switch_agent_id="test-agent",
        switch_ai_agent_name="codex-discord",
        switch_tenant_id="00000000-0000-0000-0000-000000000001",
        switch_room_ids="test-room",
        switch_reply_to_discord=True,
        _env_file=None,
    )
    receipts = MessageReceiptRepository()

    # 2. Incoming message timestamp is 3000 (newer than watermark 2000)
    new_message = IncomingMessage(
        tenant_id=settings.switch_tenant_id,
        room_id="test-room",
        message_id="msg-new-3000",
        sender="test-user",
        sender_name="User",
        body="When does the sprint end?",
        addressed=True,
        timestamp_ms=3000,
    )

    await service.ingest(new_message, settings, receipts)
    await service.queue.join()
    await service.close()

    # Post should succeed for the new message!
    assert len(posted_replies) == 1
    assert posted_replies[0]["body"] == "Sprint ends on Friday."

    # Watermark should be updated to 3000 in the file!
    assert ReplyState.load(state_file).timestamp_ms == 3000
