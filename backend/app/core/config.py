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
    switch_ingest_token: SecretStr = SecretStr("")
    switch_tenant_id: UUID | None = None
    switch_room_ids: str = Field(default="", repr=False)
    switch_backend_url: str = Field(default="http://127.0.0.1:8000", repr=False)
    log_discord_messages: bool = False

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env", extra="ignore", hide_input_in_errors=True
    )

    @property
    def allowed_switch_rooms(self) -> frozenset[str]:
        return frozenset(room.strip() for room in self.switch_room_ids.split(",") if room.strip())

    @field_validator("switch_tenant_id", mode="before")
    @classmethod
    def empty_tenant_is_unconfigured(cls, value):
        return None if value == "" else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
