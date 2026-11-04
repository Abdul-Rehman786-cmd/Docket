import asyncio
import httpx
from fastapi.testclient import TestClient
from app.main import app
from app.core.config import get_settings
from app.repositories import record_repository
from app.repositories.record_repository import list_records

client = TestClient(app)
ADMIN_HEADERS = {"X-Admin-Token": "testing-only-token"}


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_private_data_requires_auth(monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "testing-only-token")
    get_settings.cache_clear()
    for path in (
        "/api/v1/commitments",
        "/api/v1/decisions",
        "/api/v1/memories",
        "/api/v1/messages",
        "/api/v1/supabase/status",
    ):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"X-Admin-Token": "wrong"}).status_code == 401
    get_settings.cache_clear()


def test_auth_does_not_skip_database_config(monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "testing-only-token")
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "")
    get_settings.cache_clear()
    for path in (
        "/api/v1/commitments",
        "/api/v1/decisions",
        "/api/v1/memories",
        "/api/v1/messages",
        "/api/v1/supabase/status",
    ):
        response = client.get(path, headers=ADMIN_HEADERS)
        assert response.status_code == 503
        assert response.json() == {"detail": "Database is not configured"}
    get_settings.cache_clear()


def test_supabase_status_and_stored_messages_with_configured_credentials(monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "testing-only-token")
    monkeypatch.setenv("SUPABASE_URL", "https://fixture.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fixture-service-role-key")
    get_settings.cache_clear()
    calls = []

    async def fake_get(self, url, *, params=None, headers=None, timeout=10):
        calls.append((url, params, headers))
        if url.endswith("/rest/v1/tenants"):
            return httpx.Response(200, json=[{"id": "00000000-0000-0000-0000-000000000001"}], request=httpx.Request("GET", url))
        if url.endswith("/rest/v1/messages"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "00000000-0000-0000-0000-000000000099",
                        "tenant_id": "00000000-0000-0000-0000-000000000001",
                        "room_id": "00000000-0000-0000-0000-000000000010",
                        "switch_message_id": "msg-1",
                        "author_name": "Teammate",
                        "body": "Stored message text",
                        "occurred_at": "2026-11-04T12:00:00Z",
                        "extraction_status": "completed",
                        "created_at": "2026-11-04T12:00:01Z",
                    }
                ],
                request=httpx.Request("GET", url),
            )
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(record_repository.httpx.AsyncClient, "get", fake_get)

    status_resp = client.get("/api/v1/supabase/status", headers=ADMIN_HEADERS)
    assert status_resp.status_code == 200
    assert status_resp.json() == {"status": "connected"}

    messages_resp = client.get("/api/v1/messages", headers=ADMIN_HEADERS)
    assert messages_resp.status_code == 200
    assert len(messages_resp.json()) == 1
    assert messages_resp.json()[0]["switch_message_id"] == "msg-1"
    assert calls[1][1]["tenant_id"] == "eq.00000000-0000-0000-0000-000000000001"
    assert "fixture-service-role-key" not in status_resp.text
    assert "fixture-service-role-key" not in messages_resp.text


def test_supabase_error_never_exposes_credentials_or_url(monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "testing-only-token")
    monkeypatch.setenv("SUPABASE_URL", "https://fixture.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fixture-service-role-key")
    get_settings.cache_clear()

    async def failing_get(*args, **kwargs):
        raise httpx.ConnectError("https://fixture.supabase.co fixture-service-role-key failed")

    monkeypatch.setattr(record_repository.httpx.AsyncClient, "get", failing_get)
    response = client.get("/api/v1/supabase/status", headers=ADMIN_HEADERS)
    assert response.status_code == 503
    assert response.json() == {"detail": "Database request failed"}
    assert "fixture-service-role-key" not in response.text
    assert "fixture.supabase.co" not in response.text


def test_repository_allows_only_known_tables():
    try:
        asyncio.run(list_records("random_table"))
    except ValueError as exc:
        assert str(exc) == "Unknown table"
    else:
        raise AssertionError("Unknown table should be rejected")
