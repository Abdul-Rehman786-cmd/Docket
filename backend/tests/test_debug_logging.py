import logging

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.logging import configure_logging
from app.main import app
from app.repositories.record_repository import list_records


@pytest.fixture
def debug_logs(caplog):
    logger = logging.getLogger("team_memory")
    previous_level = logger.level
    configure_logging(Settings(_env_file=None, log_level="DEBUG"))
    logger.addHandler(caplog.handler)
    yield caplog
    logger.removeHandler(caplog.handler)
    logger.setLevel(previous_level)


def test_debug_setting_is_app_only_and_configuration_is_idempotent():
    logger = logging.getLogger("team_memory")
    previous_level = logger.level
    root_level = logging.getLogger().level
    http_level = logging.getLogger("httpx").level
    handlers = list(logger.handlers)
    try:
        configure_logging(Settings(_env_file=None, log_level="DEBUG"))
        configure_logging(Settings(_env_file=None, log_level="DEBUG"))
        assert logger.level == logging.DEBUG
        assert logger.handlers == handlers
        assert logging.getLogger().level == root_level
        assert logging.getLogger("httpx").level == http_level
        configure_logging(Settings(_env_file=None))
        assert not logger.isEnabledFor(logging.DEBUG)
    finally:
        logger.setLevel(previous_level)


def test_request_diagnostics_exclude_user_input(debug_logs):
    with TestClient(app) as client:
        assert client.get("/health?secret=fixture-query").status_code == 200
        assert client.get("/fixture-private-path").status_code == 404
        assert client.get(
            "/api/v1/commitments", headers={"X-Admin-Token": "fixture-private-header"}
        ).status_code == 401
        assert client.post(
            "/messages", headers={"X-Ingest-Token": "test-ingest-token"},
            json={"body": "fixture-private-body"},
        ).status_code == 422
    output = "\n".join(r.getMessage() for r in debug_logs.records if r.name.startswith("team_memory"))
    assert "route=/health status=200 duration_ms=" in output
    assert "route=unmatched status=404" in output
    assert "Admin authentication rejected" in output
    assert "Request validation rejected" in output
    for value in ("fixture-query", "fixture-private-path", "fixture-private-header", "fixture-private-body"):
        assert value not in output


def test_database_failure_diagnostics_exclude_exception_and_credentials(monkeypatch, debug_logs):
    monkeypatch.setenv("SUPABASE_URL", "https://fixture-db.invalid")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fixture-db-credential")

    def fail(*args, **kwargs):
        raise httpx.ConnectError("fixture-db-credential fixture-private-exception")

    monkeypatch.setattr(httpx, "get", fail)
    with pytest.raises(HTTPException) as error:
        list_records("commitments")
    assert error.value.status_code == 503
    assert "Record read failed table=commitments" in debug_logs.text
    for value in ("fixture-db.invalid", "fixture-db-credential", "fixture-private-exception"):
        assert value not in debug_logs.text
