from functools import lru_cache
import os
from pathlib import Path
from uuid import UUID
from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
ROOT_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    feature_logs_enabled: bool = True
    log_level: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    admin_api_token: str = Field(default="", repr=False)
    supabase_url: str = Field(
        default="",
        repr=False,
        validation_alias=AliasChoices(
            "SUPABASE_URL",
            "EXPO_PUBLIC_SUPABASE_URL",
            "NEXT_PUBLIC_SUPABASE_URL",
            "SUPABASE_PROJECT_URL",
        ),
    )
    supabase_secret_key: str = Field(
        default="",
        repr=False,
        validation_alias=AliasChoices("SUPABASE_SECRET_KEY"),
    )
    supabase_service_role_key: str = Field(
        default="",
        repr=False,
        validation_alias=AliasChoices(
            "SUPABASE_SECRET_KEY",
            "SUPABASE_SERVICE_ROLE_KEY",
            "SUPABASE_SERVICE_KEY",
            "SUPABASE_KEY",
            "SERVICE_ROLE_KEY",
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def resolve_supabase_keys(cls, data):
        if isinstance(data, dict):
            cleaned = {}
            for k, v in data.items():
                if v == "" or (isinstance(v, str) and not v.strip()):
                    continue
                cleaned[k] = v
            data = cleaned

            secret_key = (data.get("SUPABASE_SECRET_KEY") or "").strip()
            service_key = (data.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
            effective = secret_key or service_key or (data.get("SUPABASE_SERVICE_KEY") or "").strip()
            if effective:
                data["supabase_secret_key"] = effective
                data["supabase_service_role_key"] = effective
        return data
    switch_bridge_url: str = Field(default="", repr=False)
    switch_agent_id: str = Field(default="", repr=False)
    switch_agent_api_key: SecretStr = SecretStr("")
    switch_registration_token: SecretStr = SecretStr("")
    switch_requester_agent_id: str = Field(default="", repr=False)
    switch_requester_api_key: SecretStr = SecretStr("")
    switch_ai_room_id: str = Field(default="", repr=False)
    switch_ai_agent_name: str = "codex-discord"
    switch_ai_sender_id: str = Field(default="", repr=False)
    switch_ai_timeout_seconds: int = Field(default=180, ge=10, le=600)
    switch_ingest_token: SecretStr = SecretStr("")
    switch_adapter_token: SecretStr = SecretStr("")
    automatic_commitments_enabled: bool = False
    person_profiles_enabled: bool = False
    commitment_timezone: str | None = None  # Confirmed shared room zone; unset means unresolved relative dates.
    reminders_enabled: bool = False
    reminder_delivery_enabled: bool = False
    reminder_lead_minutes: int = Field(default=30, ge=0, le=10080, validation_alias=AliasChoices("REMINDER_LEAD_MINUTES"))
    reminder_lead_time: int = Field(default=30, ge=0, le=10080, validation_alias=AliasChoices("REMINDER_LEAD_TIME"))
    reminder_lead_unit: str = Field(default="minutes", validation_alias=AliasChoices("REMINDER_LEAD_UNIT"))
    switch_tenant_id: UUID | None = None
    switch_room_ids: str = Field(default="", repr=False)
    switch_ai_sender_ids: str = Field(default="", repr=False)
    switch_backend_url: str = Field(default="http://127.0.0.1:8000", repr=False)
    switch_reply_to_discord: bool = Field(default=True, validation_alias=AliasChoices("SWITCH_REPLY_TO_DISCORD"))
    database_engine: str = Field(default="", validation_alias=AliasChoices("DATABASE_ENGINE", "DB_ENGINE"))
    sqlite_db_path: str = Field(default="", validation_alias=AliasChoices("SQLITE_DB_PATH", "SQLITE_PATH"))
    storage_retention_value: int = Field(default=90, validation_alias=AliasChoices("STORAGE_RETENTION_VALUE"))
    storage_retention_unit: str = Field(default="days", validation_alias=AliasChoices("STORAGE_RETENTION_UNIT"))
    sqlite_max_space_enabled: bool = Field(default=False, validation_alias=AliasChoices("SQLITE_MAX_SPACE_ENABLED"))
    sqlite_max_space_value: float = Field(default=500.0, validation_alias=AliasChoices("SQLITE_MAX_SPACE_VALUE"))
    sqlite_max_space_unit: str = Field(default="MB", validation_alias=AliasChoices("SQLITE_MAX_SPACE_UNIT"))
    log_discord_messages: bool = False
    local_embeddings_enabled: bool = True
    document_ingestion_enabled: bool = False
    switch_document_attachments_enabled: bool = False
    switch_document_adapter_token: SecretStr = SecretStr("")
    switch_document_discord_bridge_ids: str = Field(default="",repr=False)
    switch_document_bot_sender_id: str = Field(default="",repr=False)
    switch_document_confirmation_seconds: int = Field(default=900,ge=60,le=86400)
    switch_document_max_files: int = Field(default=4,ge=1,le=8)
    switch_document_max_bytes: int = Field(default=262144,ge=1,le=262144)
    switch_document_max_text_chars: int = Field(default=262144,ge=1,le=262144)
    switch_document_download_seconds: int = Field(default=20,ge=1,le=60)
    switch_document_processing_seconds: int = Field(default=900,ge=30,le=7200)
    switch_document_max_pending: int = Field(default=200,ge=1,le=1000)
    switch_document_max_records: int = Field(default=10000,ge=10,le=100000)

    @property
    def document_discord_bridges(self) -> frozenset[str]:
        return frozenset(v.strip() for v in self.switch_document_discord_bridge_ids.split(",") if v.strip())

    @property
    def effective_database_engine(self) -> str:
        engine = (self.database_engine or "").strip().lower()
        if engine in ("sqlite", "supabase"):
            return engine
        if self.supabase_configured:
            return "supabase"
        return "sqlite"

    @property
    def sqlite_configured(self) -> bool:
        return bool(self.sqlite_db_path.strip())

    @property
    def supabase_configured(self) -> bool:
        key = self.supabase_secret_key.strip() or self.supabase_service_role_key.strip()
        return bool(self.supabase_url.strip() and key)

    @property
    def listener_agent_id(self) -> str:
        return self.switch_requester_agent_id or self.switch_agent_id

    @property
    def listener_api_key(self) -> SecretStr:
        return self.switch_requester_api_key if self.switch_requester_agent_id else self.switch_agent_api_key

    model_config = SettingsConfigDict(
        env_file=(ROOT_DIR / ".env", BACKEND_DIR / ".env"),
        extra="ignore",
        hide_input_in_errors=True,
    )

    @property
    def allowed_switch_rooms(self) -> frozenset[str]:
        return frozenset(room.strip() for room in self.switch_room_ids.split(",") if room.strip())

    @property
    def switch_ai_senders(self) -> frozenset[str]:
        # Participant IDs are opaque: use exact matches, never names or ID prefixes.
        senders = {sender.strip() for sender in self.switch_ai_sender_ids.split(",") if sender.strip()}
        if self.switch_document_bot_sender_id:
            senders.add(self.switch_document_bot_sender_id)
        if self.switch_ai_sender_id:
            senders.add(self.switch_ai_sender_id)
        if self.switch_agent_id:
            senders.add(self.switch_agent_id)
        if self.switch_requester_agent_id:
            senders.add(self.switch_requester_agent_id)
        return frozenset(senders)

    @field_validator("switch_tenant_id", mode="before")
    @classmethod
    def empty_tenant_is_unconfigured(cls, value):
        return None if value == "" else value


def _env_mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


@lru_cache
def _cached_settings(_env_stamp: tuple[int | None, int | None, bool]) -> Settings:
    if "PYTEST_CURRENT_TEST" in os.environ:
        return Settings(_env_file=None)
    return Settings()


def get_settings() -> Settings:
    in_pytest = "PYTEST_CURRENT_TEST" in os.environ
    stamp = (
        None if in_pytest else _env_mtime(ROOT_DIR / ".env"),
        None if in_pytest else _env_mtime(BACKEND_DIR / ".env"),
        in_pytest,
    )
    return _cached_settings(stamp)


get_settings.cache_clear = _cached_settings.cache_clear  # type: ignore[attr-defined]
