from functools import lru_cache
from pathlib import Path
from uuid import UUID
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    admin_api_token: str = Field(default="", repr=False)
    supabase_url: str = Field(default="", repr=False)
    supabase_service_role_key: str = Field(default="", repr=False)
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
    switch_tenant_id: UUID | None = None
    switch_room_ids: str = Field(default="", repr=False)
    switch_ai_sender_ids: str = Field(default="", repr=False)
    switch_backend_url: str = Field(default="http://127.0.0.1:8000", repr=False)
    log_discord_messages: bool = False

    @property
    def listener_agent_id(self) -> str:
        return self.switch_requester_agent_id or self.switch_agent_id

    @property
    def listener_api_key(self) -> SecretStr:
        return self.switch_requester_api_key if self.switch_requester_agent_id else self.switch_agent_api_key

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env", extra="ignore", hide_input_in_errors=True
    )

    @property
    def allowed_switch_rooms(self) -> frozenset[str]:
        return frozenset(room.strip() for room in self.switch_room_ids.split(",") if room.strip())

    @property
    def switch_ai_senders(self) -> frozenset[str]:
        # Participant IDs are opaque: use exact matches, never names or ID prefixes.
        senders = {sender.strip() for sender in self.switch_ai_sender_ids.split(",") if sender.strip()}
        if self.switch_ai_sender_id:
            senders.add(self.switch_ai_sender_id)
        return frozenset(senders)

    @field_validator("switch_tenant_id", mode="before")
    @classmethod
    def empty_tenant_is_unconfigured(cls, value):
        return None if value == "" else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
