from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app

client = TestClient(app)
HEADERS = {"X-Ingest-Token": "test-ingest-token"}


def message(index=0, **updates):
    return {
        "tenant_id": "00000000-0000-0000-0000-000000000001", "room_id": "test-room",
        "message_id": f"viewer-fixture-{index}", "sender": "fixture-sender",
        "sender_name": "Fixture teammate", "body": f"Message {index}",
        **updates,
    }


def test_homepage_serves_locked_viewer_without_private_content():
    client.post("/messages", headers=HEADERS, json=message(body="Private fixture message"))
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Incoming messages" in response.text
    assert 'id="unlock-form" method="post"' in response.text
    assert "Private fixture message" not in response.text
    assert "test-ingest-token" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_viewer_assets_are_available_without_embedding_credentials():
    for path in ("/viewer.css", "/viewer.js"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "test-ingest-token" not in response.text


def test_feed_returns_redacted_previews_and_never_caches(monkeypatch):
    monkeypatch.setenv("FIXTURE_API_KEY", "fixture-viewer-credential")
    response = client.post("/messages", headers=HEADERS, json=message(
        body="Hello Discord! fixture-viewer-credential test-ingest-token"
    ))
    assert response.status_code == 200
    feed = client.get("/messages/recent", headers=HEADERS)
    assert feed.status_code == 200
    assert feed.headers["cache-control"] == "no-store"
    assert feed.json()[0]["message_ref"] == response.json()["message_ref"]
    assert feed.json()[0]["text"] == "Hello Discord! [REDACTED] [REDACTED]"
    assert feed.json()[0]["sender_name"] == "Fixture teammate"
    assert "fixture-viewer-credential" not in feed.text
    assert "test-ingest-token" not in feed.text
    unauthorized = client.get("/messages/recent")
    assert unauthorized.status_code == 401
    assert unauthorized.headers["cache-control"] == "no-store"
    assert "Hello Discord!" not in unauthorized.text


def test_feed_scopes_history_to_current_tenant_and_allowed_rooms(monkeypatch):
    client.post("/messages", headers=HEADERS, json=message())
    monkeypatch.setenv("SWITCH_TENANT_ID", "00000000-0000-0000-0000-000000000002")
    get_settings.cache_clear()
    assert client.get("/messages/recent", headers=HEADERS).json() == []
    assert client.get("/messages/status", headers=HEADERS).json() == {"sender_name": None, "message": None}
    monkeypatch.setenv("SWITCH_TENANT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("SWITCH_ROOM_IDS", "second-room")
    get_settings.cache_clear()
    assert client.get("/messages/recent", headers=HEADERS).json() == []
    assert client.get("/messages/status", headers=HEADERS).json() == {"sender_name": None, "message": None}


def test_feed_history_is_bounded_newest_first_and_deduplicated():
    for index in range(102):
        response = client.post("/messages", headers=HEADERS, json=message(index))
        assert response.status_code == 200
    client.post("/messages", headers=HEADERS, json=message(101))
    feed = client.get("/messages/recent", headers=HEADERS).json()
    assert len(feed) == 100
    assert feed[0]["text"] == "Message 101"
    assert feed[-1]["text"] == "Message 2"


def test_empty_or_unconfigured_feed_is_explicit(monkeypatch):
    assert client.get("/messages/recent", headers=HEADERS).json() == []
    monkeypatch.setenv("SWITCH_ROOM_IDS", "")
    get_settings.cache_clear()
    assert client.get("/messages/recent", headers=HEADERS).status_code == 503


def test_latest_status_returns_sender_and_message_instead_of_counters():
    assert client.get("/messages/status", headers=HEADERS).json() == {"sender_name": None, "message": None}
    client.post("/messages", headers=HEADERS, json=message(sender_name="Ali", body="Hello team"))
    client.post("/messages", headers=HEADERS, json=message(1, sender_name="Sara", body="Report is ready"))
    response = client.get("/messages/status", headers=HEADERS)
    assert response.json() == {"sender_name": "Sara", "message": "Report is ready"}
    assert response.headers["cache-control"] == "no-store"


def test_sender_names_are_redacted_before_storage_and_status(monkeypatch):
    monkeypatch.setenv("FIXTURE_API_KEY", "fixture-private-display-name")
    response = client.post("/messages", headers=HEADERS, json=message(
        sender_name="fixture-private-display-name", body="test-ingest-token"
    ))
    assert response.status_code == 200
    latest = client.get("/messages/status", headers=HEADERS)
    assert latest.json() == {"sender_name": "[REDACTED]", "message": "[REDACTED]"}
    assert client.get("/messages/recent", headers=HEADERS).json()[0]["sender_name"] == "[REDACTED]"
