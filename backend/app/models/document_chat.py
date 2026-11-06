"""Untrusted AI routing proposals; identities and document IDs are never inputs."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DocumentIntent(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True, hide_input_in_errors=True)

    action: Literal['none', 'list', 'more', 'find', 'select', 'ask', 'summarize', 'clear', 'clarify']
    reference: str | None = Field(default=None, min_length=1, max_length=200)
    choice: int | None = Field(default=None, ge=1, le=5)
    use_selected: bool = False
    query: str | None = Field(default=None, min_length=1, max_length=4000)

    @model_validator(mode='after')
    def consistent_target(self):
        if sum((self.reference is not None, self.choice is not None, self.use_selected)) > 1:
            raise ValueError('Choose one document target')
        if self.action in {'none', 'list', 'more', 'clear', 'clarify'} and (
            self.reference is not None or self.choice is not None or self.use_selected or self.query is not None):
            raise ValueError('This action takes no target or query')
        if self.action == 'select' and not (self.reference or self.choice):
            raise ValueError('Selection needs a name or displayed choice')
        if self.action == 'select' and (self.use_selected or self.query is not None):
            raise ValueError('Selection takes only a name or displayed choice')
        if self.action == 'find' and not (self.reference or self.query):
            raise ValueError('Discovery needs a name or query')
        if self.action == 'find' and (self.choice is not None or self.use_selected):
            raise ValueError('Discovery takes only a name or query')
        if self.action in {'ask', 'summarize'} and not (self.reference or self.choice or self.use_selected or self.query):
            raise ValueError('A document question needs a target or discovery query')
        return self
