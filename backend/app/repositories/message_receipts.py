from collections import OrderedDict, deque
from datetime import datetime, timezone
from threading import Lock
from uuid import UUID
from typing import Literal

from app.models.messages import DeliveryStatistics, MessagePreview, MessageReceipt


class MessageReceiptRepository:
    """Bounded, process-local receipts and redacted previews. Never stores raw text."""

    def __init__(self, capacity: int = 1000):
        self._capacity = capacity
        self._receipts: OrderedDict[str, MessageReceipt] = OrderedDict()
        self._lock = Lock()
        self._received_count = 0
        self._duplicate_count = 0
        self._last_receipt: MessageReceipt | None = None
        self._previews: deque[tuple[UUID, str, MessagePreview]] = deque(maxlen=100)

    def record(
        self, message_ref: str, *, tenant_id: UUID, room_id: str,
        redacted_sender_name: str, redacted_text: str,
        sender_kind: Literal["message", "ai"] = "message", thread_ref: str | None = None,
    ) -> MessageReceipt:
        with self._lock:
            existing = self._receipts.get(message_ref)
            if existing:
                self._duplicate_count += 1
                return existing.model_copy(update={"duplicate": True})
            receipt = MessageReceipt(message_ref=message_ref, received_at=datetime.now(timezone.utc))
            self._receipts[message_ref] = receipt
            self._received_count += 1
            self._last_receipt = receipt
            self._previews.append((tenant_id, room_id, MessagePreview(
                message_ref=message_ref, received_at=receipt.received_at,
                sender_name=redacted_sender_name, text=redacted_text,
                sender_kind=sender_kind, thread_ref=thread_ref,
            )))
            if len(self._receipts) > self._capacity:
                self._receipts.popitem(last=False)
            return receipt

    def recent(self, tenant_id: UUID, allowed_rooms: frozenset[str]) -> list[MessagePreview]:
        with self._lock:
            return [
                preview.model_copy() for tenant, room, preview in reversed(self._previews)
                if tenant == tenant_id and room in allowed_rooms
            ]

    def status(self) -> DeliveryStatistics:
        with self._lock:
            return DeliveryStatistics(
                received_count=self._received_count,
                duplicate_count=self._duplicate_count,
                last_message_ref=self._last_receipt.message_ref if self._last_receipt else None,
                last_received_at=self._last_receipt.received_at if self._last_receipt else None,
            )
