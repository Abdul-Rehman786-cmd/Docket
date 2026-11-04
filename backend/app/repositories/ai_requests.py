"""Bounded redacted results, keyed by authorized scope. No raw prompts or credentials."""

from collections import OrderedDict
from datetime import datetime, timezone
from uuid import UUID

from app.models.ai import AIResponse


class AIRequestRepository:
    def __init__(self):
        self.rows: OrderedDict[tuple, AIResponse] = OrderedDict()

    def get(self, scope: tuple, request_id: UUID) -> AIResponse | None:
        result = self.rows.get((scope, request_id))
        return result.model_copy() if result else None

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
        key = (scope, request_id)
        self.rows[key] = self.rows[key].model_copy(update=values)
