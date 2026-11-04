"""Bounded redacted results, keyed by authorized scope. No raw prompts or credentials."""

from collections import OrderedDict
from datetime import datetime, timezone
from uuid import UUID

from app.models.ai import AIResponse


class AIRequestRepository:
    def __init__(self):
        self.rows: OrderedDict[tuple, AIResponse] = OrderedDict()
        self.aliases: dict[tuple, UUID] = {}

    def _resolve_key(self, scope: tuple, request_id: UUID) -> tuple:
        target_id = self.aliases.get((scope, request_id), request_id)
        return (scope, target_id)

    def get(self, scope: tuple, request_id: UUID) -> AIResponse | None:
        result = self.rows.get(self._resolve_key(scope, request_id))
        return result.model_copy() if result else None

    def get_by_source(self, scope: tuple, source_message_ref: str) -> AIResponse | None:
        for (key_scope, _), row in reversed(self.rows.items()):
            if key_scope == scope and row.source_message_ref == source_message_ref:
                return row.model_copy()
        return None

    def rekey(self, scope: tuple, old_request_id: UUID, new_request_id: UUID) -> AIResponse:
        old_key = self._resolve_key(scope, old_request_id)
        row = self.rows.pop(old_key)
        updated = row.model_copy(update={"request_id": new_request_id})
        self.rows[(scope, new_request_id)] = updated
        self.aliases[(scope, old_request_id)] = new_request_id
        self.aliases[old_key] = new_request_id
        return updated.model_copy()

    def recent(self, scope: tuple) -> list[AIResponse]:
        return [row.model_copy() for (key, _), row in reversed(self.rows.items()) if key == scope]

    def create(self, scope: tuple, request_id: UUID, question: str, source: str | None) -> AIResponse:
        if len(self.rows) >= 100:
            disposable = next((key for key, row in self.rows.items() if row.status in ("answered", "failed")), None)
            if disposable is None:
                raise ValueError("Request history is full")
            del self.rows[disposable]
        row = AIResponse(request_id=request_id, source_message_ref=source, question=question,
                         created_at=datetime.now(timezone.utc))
        self.rows[(scope, request_id)] = row
        return row.model_copy()

    def update(self, scope: tuple, request_id: UUID, **values) -> None:
        key = self._resolve_key(scope, request_id)
        if key in self.rows:
            self.rows[key] = self.rows[key].model_copy(update=values)
