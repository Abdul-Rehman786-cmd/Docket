import pytest

from app.core.config import get_settings
from app.core import message_logging
from app.main import app
from app.repositories.message_receipts import MessageReceiptRepository


@pytest.fixture(autouse=True)
def isolate_local_configuration(monkeypatch):
    """Tests must never use the developer's real service credentials."""
    for name, value in {
        "ADMIN_API_TOKEN": "",
        "SUPABASE_URL": "",
        "SUPABASE_SERVICE_ROLE_KEY": "",
        "SWITCH_BRIDGE_URL": "http://127.0.0.1",
        "SWITCH_AGENT_ID": "test-agent",
        "SWITCH_AGENT_API_KEY": "test-agent-key",
        "SWITCH_INGEST_TOKEN": "test-ingest-token",
        "SWITCH_TENANT_ID": "00000000-0000-0000-0000-000000000001",
        "SWITCH_ROOM_IDS": "test-room,second-room",
        "SWITCH_BACKEND_URL": "http://127.0.0.1:8000",
        "LOG_DISCORD_MESSAGES": "false",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(message_logging, "dotenv_values", lambda path: {})
    get_settings.cache_clear()
    app.state.message_receipts = MessageReceiptRepository()
    yield
    get_settings.cache_clear()
