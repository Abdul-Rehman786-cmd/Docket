from pathlib import Path
from dotenv import dotenv_values
from app.core.config import BACKEND_DIR, get_settings

ENV_PATH = BACKEND_DIR / ".env"
EXAMPLE_PATH = BACKEND_DIR / ".env.example"

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
]

OPTIONAL_KEYS = {
    "DATABASE_ENGINE": "Optional · defaults to sqlite",
    "SQLITE_DB_PATH": "Optional · defaults to backend/.local/team_memory.db",
    "ADMIN_API_TOKEN": "Optional · for admin API endpoints",
    "SWITCH_AI_TIMEOUT_SECONDS": "Optional · defaults to 180s",
    "SWITCH_REPLY_TO_DISCORD": "Optional · defaults to true",
    "SWITCH_AI_AGENT_NAME": "Optional · defaults to codex-discord",
    "LOG_DISCORD_MESSAGES": "Optional · defaults to false",
    "SWITCH_AI_SENDER_IDS": "Optional · extra agent IDs",
    "GROK_API_KEY": "Optional · external model API key",
    "SUPABASE_SERVICE_ROLE_KEY": "Optional · legacy alias for SUPABASE_SECRET_KEY",
}


def ensure_env_file() -> Path:
    if not ENV_PATH.exists():
        ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
        if EXAMPLE_PATH.exists():
            ENV_PATH.write_text(EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            ENV_PATH.write_text("\n".join(f"{k}=" for k in DEFAULT_KEYS) + "\n", encoding="utf-8")
    return ENV_PATH


def read_env_variables() -> dict[str, str]:
    path = ensure_env_file()
    raw = dotenv_values(path)
    result: dict[str, str] = {}
    # Prioritize keys that already exist in .env
    for k, v in raw.items():
        if k:
            result[k] = v or ""
    # Append any standard optional keys not yet defined
    for k in DEFAULT_KEYS:
        if k not in result:
            result[k] = ""
    return result


def write_env_variables(variables: dict[str, str]) -> None:
    ensure_env_file()
    existing = dotenv_values(ENV_PATH)
    merged = {**existing, **variables}
    lines = ["# Team Memory environment configuration", ""]
    for k, v in merged.items():
        if not k:
            continue
        val = str(v) if v is not None else ""
        lines.append(f"{k}={val}")
    temp_path = ENV_PATH.with_suffix(".tmp")
    temp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp_path.replace(ENV_PATH)
    get_settings.cache_clear()

