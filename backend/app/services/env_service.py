from pathlib import Path
from dotenv import dotenv_values, set_key
from threading import RLock
import re

from fastapi import HTTPException
from app.core.config import BACKEND_DIR, get_settings

ENV_PATH = BACKEND_DIR / ".env"
EXAMPLE_PATH = BACKEND_DIR / ".env.example"
_ENV_LOCK = RLock()
SECRET_KEY = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|DATABASE_URL|CONNECTION_STRING)", re.I)

DEFAULT_KEYS = [
    "DATABASE_ENGINE",
    "SQLITE_DB_PATH",
    "SUPABASE_URL",
    "SUPABASE_SECRET_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "SWITCH_INGEST_TOKEN",
    "SWITCH_BRIDGE_URL",
    "SWITCH_TENANT_ID",
    "SWITCH_ROOM_IDS",
    "SWITCH_AGENT_ID",
    "SWITCH_AGENT_API_KEY",
    "SWITCH_REQUESTER_AGENT_ID",
    "SWITCH_REQUESTER_API_KEY",
    "SWITCH_AI_ROOM_ID",
    "SWITCH_AI_AGENT_NAME",
    "SWITCH_AI_SENDER_ID",
    "SWITCH_AI_TIMEOUT_SECONDS",
    "SWITCH_REPLY_TO_DISCORD",
    "SWITCH_BACKEND_URL",
    "ADMIN_API_TOKEN",
    "LOG_DISCORD_MESSAGES",
    "STORAGE_RETENTION_VALUE",
    "STORAGE_RETENTION_UNIT",
    "SQLITE_MAX_SPACE_ENABLED",
    "SQLITE_MAX_SPACE_VALUE",
    "SQLITE_MAX_SPACE_UNIT",
    "REMINDERS_ENABLED",
    "REMINDER_DELIVERY_ENABLED",
    "REMINDER_LEAD_MINUTES",
    "REMINDER_LEAD_TIME",
    "REMINDER_LEAD_UNIT",
]

def ensure_env_file() -> Path:
    if not ENV_PATH.exists():
        ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
        if EXAMPLE_PATH.exists():
            ENV_PATH.write_text(EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            ENV_PATH.write_text("\n".join(f"{k}=" for k in DEFAULT_KEYS) + "\n", encoding="utf-8")
    return ENV_PATH


def settings_keys():
    """Expose only documented keys, never arbitrary entries from a real .env."""
    examples = dotenv_values(EXAMPLE_PATH, interpolate=False) if EXAMPLE_PATH.exists() else {}
    return list(dict.fromkeys([*DEFAULT_KEYS, *examples]))


def read_env_variables():
    """Return editable non-secret values and presence flags for write-only credentials."""
    with _ENV_LOCK:
        values = dotenv_values(ensure_env_file(), interpolate=False)
    variables, credentials = {}, {}
    for key in settings_keys():
        value = values.get(key) or ""
        if SECRET_KEY.search(key):
            credentials[key] = bool(value.strip())
        else:
            variables[key] = value
    return {"variables": variables, "credentials": credentials}


def save_viewer_settings(variables):
    allowed = set(settings_keys())
    if any(key not in allowed for key in variables):
        raise HTTPException(422, "Unsupported setting name")
    updates = {}
    for key, wrapped in variables.items():
        value = wrapped.get_secret_value()
        if len(value) > 16384 or any(char in value for char in ("\r", "\n", "\x00")):
            raise HTTPException(422, "Setting values must be single-line text")
        # Empty credential inputs mean retain the existing secret, not erase it.
        if SECRET_KEY.search(key) and not value.strip():
            continue
        updates[key] = value
    write_env_variables(updates)


def write_env_variables(variables: dict[str, str]) -> None:
    """Quote values and atomically replace the local file after all updates succeed."""
    with _ENV_LOCK:
        path = ensure_env_file()
        temp_path = path.with_suffix(".tmp")
        try:
            temp_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            for key, value in variables.items():
                set_key(str(temp_path), key, str(value) if value is not None else "", quote_mode="auto")
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)
        get_settings.cache_clear()
