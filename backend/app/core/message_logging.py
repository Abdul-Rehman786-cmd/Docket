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
OPAQUE_TOKEN = re.compile(
    r"\b(?:sk-|gsk_|xox[baprs]-|gh[pousr]_|github_pat_|AIza|AKIA|ASIA)[A-Za-z0-9_.-]{8,}"
    r"|\b[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{15,}\b"
    r"|(?<![\w])[A-Za-z0-9_+/=-]{32,}(?![\w])"
)


def message_text_for_log(body: str, settings: Settings) -> str:
    try:
        configured = {
            settings.admin_api_token, settings.supabase_service_role_key,
            settings.switch_agent_api_key.get_secret_value(),
            settings.switch_ingest_token.get_secret_value(),
            settings.switch_registration_token.get_secret_value(),
            settings.switch_requester_api_key.get_secret_value(),
        }
        # Include credentials not otherwise used by the receiver, such as LLM keys.
        for source in (os.environ, dotenv_values(BACKEND_DIR / ".env")):
            configured.update(value for name, value in source.items() if SECRET_NAME.search(name) and value)
        text = body
        for value in sorted(configured - {""}, key=len, reverse=True):
            text = text.replace(value, REDACTED)
        text = PRIVATE_KEY.sub(REDACTED, text)
        text = URL.sub(REDACTED, text)
        text = BEARER.sub(lambda match: match[1] + " " + REDACTED, text)
        text = CREDENTIAL.sub(lambda match: match["label"] + REDACTED, text)
        text = OPAQUE_TOKEN.sub(REDACTED, text)
        if len(text) > 2000:
            text = text[:2000] + "... [truncated]"
        # Escape newlines and control characters so chat cannot forge terminal log entries.
        return json.dumps(text, ensure_ascii=True)
    except Exception:
        # Failure to redact must never turn into raw message logging or fail ingestion.
        return json.dumps("[message text suppressed: redaction unavailable]")
