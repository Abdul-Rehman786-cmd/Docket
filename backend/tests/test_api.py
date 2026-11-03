from fastapi.testclient import TestClient
from app.main import app
from app.core.config import get_settings
from app.repositories.record_repository import list_records

client = TestClient(app)


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_private_data_requires_auth(monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "testing-only-token")
    get_settings.cache_clear()
    assert client.get("/api/v1/commitments").status_code == 401
    assert client.get("/api/v1/decisions", headers={"X-Admin-Token": "wrong"}).status_code == 401
    get_settings.cache_clear()


def test_auth_does_not_skip_database_config(monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "testing-only-token")
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "")
    get_settings.cache_clear()
    response = client.get("/api/v1/commitments", headers={"X-Admin-Token": "testing-only-token"})
    assert response.status_code == 503
    assert response.json() == {"detail": "Database is not configured"}
    get_settings.cache_clear()


def test_repository_allows_only_known_tables():
    try:
        list_records("random_table")
    except ValueError as exc:
        assert str(exc) == "Unknown table"
    else:
        raise AssertionError("Unknown table should be rejected")
