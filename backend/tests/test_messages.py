import logging

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.repositories import record_repository

client = TestClient(app)
HEADERS = {"X-Ingest-Token": "test-ingest-token"}


def payload(**updates):
    return {
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "room_id": "test-room", "message_id": "test-message", "sender": "test-sender",
        "body": "Confidential fixture text", "timestamp_ms": 1793788800000,
        **updates,
    }


@pytest.mark.parametrize("method,path", [
    ("post", "/messages"), ("get", "/messages/status"), ("get", "/messages/recent"),
])
@pytest.mark.parametrize("headers", [{}, {"X-Ingest-Token": "wrong"}, {"X-Admin-Token": "test-ingest-token"}])
def test_message_endpoints_require_dedicated_credential(method, path, headers):
    response = client.request(method, path, headers=headers, json=payload() if method == "post" else None)
    assert response.status_code == 401


@pytest.mark.parametrize("updates", [
    {"tenant_id": "00000000-0000-0000-0000-000000000002"},
    {"room_id": "unmapped-room"},
])
def test_rejects_unmapped_tenant_or_room(updates):
    response = client.post("/messages", headers=HEADERS, json=payload(**updates))
    assert response.status_code == 403
    assert client.get("/messages/status", headers=HEADERS).json() == {"sender_name": None, "message": None}


def test_receipt_confirms_delivery_and_does_not_expose_content(caplog):
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        response = client.post("/messages", headers=HEADERS, json=payload())
    assert response.status_code == 200
    receipt = response.json()
    assert receipt["status"] == "received"
    assert len(receipt["message_ref"]) == 24
    assert receipt["duplicate"] is False
    assert receipt["message_ref"] in caplog.text
    for sensitive in (payload()["body"], payload()["sender"], HEADERS["X-Ingest-Token"]):
        assert sensitive not in response.text
        assert sensitive not in caplog.text
    status = client.get("/messages/status", headers=HEADERS).json()
    assert status == {"sender_name": "Unknown sender", "message": payload()["body"]}


def test_replayed_message_is_acknowledged_without_incrementing_delivery_count():
    first = client.post("/messages", headers=HEADERS, json=payload()).json()
    second = client.post("/messages", headers=HEADERS, json=payload()).json()
    assert second["message_ref"] == first["message_ref"]
    assert second["received_at"] == first["received_at"]
    assert second["duplicate"] is True
    assert app.state.message_receipts.status().duplicate_count == 1
    assert app.state.message_receipts.status().received_count == 1
    other_room = client.post("/messages", headers=HEADERS, json=payload(room_id="second-room")).json()
    assert other_room["message_ref"] != first["message_ref"]


@pytest.mark.parametrize("updates", [
    {"body": {"private": "sensitive-fixture"}},
    {"sensitive-fixture": "unexpected field"},
    {"message_id": ""},
    {"body": "x" * 64001},
])
def test_validation_never_echoes_invalid_message_input(updates):
    response = client.post("/messages", headers=HEADERS, json=payload(**updates))
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid request payload"}


def test_missing_receiver_configuration_fails_closed(monkeypatch):
    monkeypatch.setenv("SWITCH_ROOM_IDS", "")
    get_settings.cache_clear()
    response = client.post("/messages", headers=HEADERS, json=payload())
    assert response.status_code == 503


def test_missing_ingest_token_cannot_authenticate(monkeypatch):
    monkeypatch.setenv("SWITCH_INGEST_TOKEN", "")
    get_settings.cache_clear()
    assert client.post("/messages", headers=HEADERS, json=payload()).status_code == 401


def test_debug_mode_prints_message_in_api_terminal(monkeypatch, caplog):
    monkeypatch.setenv("LOG_DISCORD_MESSAGES", "true")
    get_settings.cache_clear()
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        response = client.post("/messages", headers=HEADERS, json=payload(body="Hello from Discord!"))
    assert response.status_code == 200
    assert 'text="Hello from Discord!"' in caplog.text
    assert response.json()["message_ref"] in caplog.text
    assert "Hello from Discord!" not in response.text


def test_unauthorized_message_is_never_printed_even_in_debug_mode(monkeypatch, caplog):
    monkeypatch.setenv("LOG_DISCORD_MESSAGES", "true")
    get_settings.cache_clear()
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        response = client.post("/messages", json=payload(body="Unauthorized fixture message"))
    assert response.status_code == 401
    assert "Unauthorized fixture message" not in caplog.text


def test_debug_message_redacts_credentials(monkeypatch, caplog):
    monkeypatch.setenv("LOG_DISCORD_MESSAGES", "true")
    monkeypatch.setenv("FIXTURE_API_KEY", "fixture-configured-credential")
    get_settings.cache_clear()
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        response = client.post("/messages", headers=HEADERS, json=payload(
            body="Hello team, fixture-configured-credential and test-ingest-token"
        ))
    assert response.status_code == 200
    assert "Hello team," in caplog.text
    assert "fixture-configured-credential" not in caplog.text
    assert "test-ingest-token" not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_post_messages_does_not_unconditionally_write_to_supabase(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://fixture.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fixture-service-role-key")
    get_settings.cache_clear()

    def unexpected_db_call(*args, **kwargs):
        raise AssertionError("POST /messages must not write directly to Supabase before LLM decision")

    monkeypatch.setattr(record_repository.httpx, "get", unexpected_db_call)
    monkeypatch.setattr(record_repository.httpx, "post", unexpected_db_call)

    response = client.post("/messages", headers=HEADERS, json=payload())
    assert response.status_code == 200
    assert response.json()["status"] == "received"
