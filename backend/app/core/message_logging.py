"""Redacted, single-line message previews for opt-in local debugging."""

import json
import os
import re

from dotenv import dotenv_values

from app.core.config import BACKEND_DIR, Settings

REDACTED = "[REDACTED]"
SECRET_NAME = re.compile(r"key|token|password|passwd|secret|credential|dsn|connection|database_url", re.I)
PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S
)
URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s<>\"']+", re.I)
BEARER = re.compile(r"\b(Bearer|Basic)\s+[^\s,;\"']+", re.I)
CREDENTIAL = re.compile(
    r"(?P<label>[\"']?\b[\w.-]*(?:key|token|password|passwd|pwd|secret|credential|dsn|connection[_ -]?string|database[_ -]?url)"
    r"[\"']?\s*(?:[:=]|\bis\b)\s*)(?:\"[^\"]*\"|'[^']*'|[^\r\n]+)", re.I
)
KNOWN_TOKEN_PATTERN = (
    r"\b(?:sk-|gsk_|xox[baprs]-|gh[pousr]_|github_pat_|AIza|AKIA|ASIA)[A-Za-z0-9_.-]{8,}"
    r"|\b[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{15,}\b"
)
OPAQUE_TOKEN = re.compile(KNOWN_TOKEN_PATTERN+r"|(?<![\w])[A-Za-z0-9_+/=-]{32,}(?![\w])")
IDENTIFIER_TOKEN = re.compile(KNOWN_TOKEN_PATTERN+r"|\bsb_(?:secret|publishable)_[A-Za-z0-9_-]+")


def _configured_secrets(settings):
    configured = {
        settings.admin_api_token,settings.supabase_service_role_key,settings.switch_adapter_token.get_secret_value(),
        settings.switch_agent_api_key.get_secret_value(),settings.switch_ingest_token.get_secret_value(),
        settings.switch_registration_token.get_secret_value(),settings.switch_requester_api_key.get_secret_value(),
    }
    for source in (os.environ,dotenv_values(BACKEND_DIR/".env")):
        configured.update(value for name,value in source.items() if SECRET_NAME.search(name) and value)
    return {v for v in configured if isinstance(v,str) and v.strip()}


def redact_source_identifier(value: str | None,settings: Settings) -> str | None:
    """Preserve opaque source IDs; broad chat-token heuristics would mask UUIDs.

    Known credentials, explicit token formats and URLs still fail closed.
    """
    if value is None:
        return None
    try:
        if not isinstance(value,str) or not value or len(value)>512 or re.search(r"\s",value):
            return None
        text=value
        for secret in sorted(_configured_secrets(settings),key=len,reverse=True):
            text=text.replace(secret,REDACTED)
        text=URL.sub(REDACTED,text)
        text=IDENTIFIER_TOKEN.sub(REDACTED,text)
        text=CREDENTIAL.sub(lambda m:m['label']+REDACTED,text)
        return text
    except Exception:
        return None


def message_text_for_log(body: str | None, settings: Settings, *, max_length: int = 2000) -> str:
    try:
        text = str(body or "")
        clean_secrets = _configured_secrets(settings)
        for value in sorted(clean_secrets, key=len, reverse=True):
            text = text.replace(value, REDACTED)
        text = PRIVATE_KEY.sub(REDACTED, text)
        text = URL.sub(REDACTED, text)
        text = BEARER.sub(lambda match: match[1] + " " + REDACTED, text)
        text = CREDENTIAL.sub(lambda match: match["label"] + REDACTED, text)
        text = OPAQUE_TOKEN.sub(REDACTED, text)
        if len(text) > max_length:
            text = text[:max_length] + "... [truncated]"
        # Escape newlines and control characters so chat cannot forge terminal log entries.
        return json.dumps(text, ensure_ascii=True)
    except Exception:
        # Failure to redact must never turn into raw message logging or fail ingestion.
        return json.dumps("[message text suppressed: redaction unavailable]")
