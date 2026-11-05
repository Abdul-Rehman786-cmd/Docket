from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ViewerAuthentication(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    type: Literal["authenticate"]
    token: SecretStr = Field(min_length=1, max_length=4096)


class EnvVariablesPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    variables: dict[str, str] = Field(default_factory=dict)
