import json

import pytest

from app.core.config import get_settings
from app.core import message_logging


@pytest.mark.parametrize("text,hidden", [
    ("password: fixture password with spaces", "fixture password with spaces"),
    ('{"api_key": "fixture-key-value"}', "fixture-key-value"),
    ("My password is 'fixture password'", "fixture password"),
    ("Authorization: Bearer fixture-bearer-value", "fixture-bearer-value"),
    ("postgresql://fixture-user:fixture-password@db.invalid/example", "fixture-password"),
    ("https://example.invalid/?token=fixture-query-value", "fixture-query-value"),
    ("sk-fixture-credential-value", "sk-fixture-credential-value"),
    ("eyJmaXh0dXJlIn0.eyJmaXh0dXJlIn0.Zml4dHVyZS1zaWduYXR1cmU", "Zml4dHVyZS1zaWduYXR1cmU"),
    ("-----BEGIN PRIVATE KEY-----\nfixture-private-key\n-----END PRIVATE KEY-----", "fixture-private-key"),
])
def test_masks_common_credential_forms(text, hidden):
    result = message_logging.message_text_for_log(text, get_settings())
    assert hidden not in result
    assert "[REDACTED]" in result


def test_masks_other_credentials_in_local_dotenv(monkeypatch):
    monkeypatch.setattr(message_logging, "dotenv_values", lambda path: {"GROK_API_KEY": "fixture-dotenv-credential"})
    result = message_logging.message_text_for_log("Hello fixture-dotenv-credential", get_settings())
    assert json.loads(result) == "Hello [REDACTED]"


def test_control_characters_cannot_create_terminal_log_entries():
    result = message_logging.message_text_for_log("Hello\n\x1b[31mforged entry", get_settings())
    assert "\n" not in result and "\x1b" not in result
    assert "\\n" in result and "\\u001b" in result


def test_failure_to_load_redaction_values_suppresses_text(monkeypatch):
    def unavailable(path):
        raise OSError("Fixture read failure")

    monkeypatch.setattr(message_logging, "dotenv_values", unavailable)
    result = message_logging.message_text_for_log("Private fixture text", get_settings())
    assert json.loads(result) == "[message text suppressed: redaction unavailable]"
