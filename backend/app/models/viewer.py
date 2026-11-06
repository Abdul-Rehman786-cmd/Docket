from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ViewerAuthentication(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    type: Literal["authenticate"]
    token: SecretStr = Field(min_length=1, max_length=4096)


class EnvVariablesPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    variables: dict[str, SecretStr] = Field(default_factory=dict, max_length=200, repr=False)


class PruneByTimelinePayload(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    engine: Literal["supabase", "sqlite", "active"] = "active"
    retention_value: int = Field(default=90, ge=0, le=36500)
    retention_unit: Literal["minutes", "hours", "days", "weeks", "months", "years", "indefinite"] = "days"
    cutoff_iso: str | None = None


class EnforceSqliteSpacePayload(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    max_space_value: float = Field(gt=0, le=1000000)
    max_space_unit: Literal["KB", "MB", "GB"] = "MB"
    enabled: bool = True


class ReminderConfigPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    enabled: bool = False
    lead_time: int = Field(default=30, ge=0, le=10080)
    lead_unit: Literal["minutes", "hours", "days"] = "minutes"
    lead_minutes: int | None = Field(default=None, ge=0, le=10080)
    delivery_enabled: bool = True

