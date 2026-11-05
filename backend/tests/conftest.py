import pytest
import httpx

from app.core.config import get_settings
from app.core import message_logging
from app.main import app
from app.repositories.message_receipts import MessageReceiptRepository
from app.services.ai_service import AIService


@pytest.fixture(autouse=True)
def isolate_local_configuration(monkeypatch, tmp_path):
    """Tests must never use the developer's real service credentials."""
    async def blocked_network(self, request):
        raise AssertionError("External HTTP requires an explicit test transport")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_network)
    monkeypatch.setattr("app.repositories.ingestion_jobs.JOURNAL_PATH", tmp_path / "ingestion.sqlite3")
    monkeypatch.setattr("app.services.ai_service.DEFAULT_REPLY_STATE_PATH", tmp_path / "last_replied_message.txt")
    for name, value in {
        "LOG_LEVEL": "INFO",
        "ADMIN_API_TOKEN": "",
        "SUPABASE_URL": "",
        "SUPABASE_SERVICE_ROLE_KEY": "",
        "SWITCH_BRIDGE_URL": "http://127.0.0.1",
        "SWITCH_AGENT_ID": "test-agent",
        "SWITCH_AGENT_API_KEY": "test-agent-key",
        "SWITCH_INGEST_TOKEN": "test-ingest-token",
        "SWITCH_TENANT_ID": "00000000-0000-0000-0000-000000000001",
        "SWITCH_ROOM_IDS": "test-room,second-room",
        "SWITCH_AI_SENDER_IDS": "fixture-ai-participant",
        "SWITCH_REGISTRATION_TOKEN": "",
        "SWITCH_REQUESTER_AGENT_ID": "",
        "SWITCH_REQUESTER_API_KEY": "",
        "SWITCH_AI_ROOM_ID": "",
        "SWITCH_AI_SENDER_ID": "",
        "SWITCH_AI_AGENT_NAME": "codex-discord",
        "SWITCH_AI_TIMEOUT_SECONDS": "180",
        "SWITCH_BACKEND_URL": "http://127.0.0.1:8000",
        "LOG_DISCORD_MESSAGES": "false",
        "LOCAL_EMBEDDINGS_ENABLED": "false",
        "SWITCH_REPLY_TO_DISCORD": "true",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(message_logging, "dotenv_values", lambda path: {})
    get_settings.cache_clear()
    app.state.message_receipts = MessageReceiptRepository()
    app.state.ai_service = AIService()
    yield
    get_settings.cache_clear()
