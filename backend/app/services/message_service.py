from datetime import datetime, timezone
import hashlib
import json
import logging

from fastapi import HTTPException

from app.core.config import Settings
from app.core.message_logging import message_text_for_log
from app.models.messages import IncomingMessage, MessagePreview, MessageReceipt, MessageStatus
from app.repositories.message_receipts import MessageReceiptRepository
from app.repositories.record_repository import persist_message

logger = logging.getLogger("uvicorn.error")


def message_reference(message: IncomingMessage) -> str:
    identity = json.dumps([str(message.tenant_id), message.room_id, message.message_id])
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def receive_message(
    message: IncomingMessage, settings: Settings, receipts: MessageReceiptRepository
) -> MessageReceipt:
    if settings.switch_tenant_id is None or not settings.allowed_switch_rooms:
        raise HTTPException(503, "Switch ingestion is not configured")
    if message.tenant_id != settings.switch_tenant_id or message.room_id not in settings.allowed_switch_rooms:
        raise HTTPException(403, "Message tenant or room is not authorized")
    safe_text = message_text_for_log(message.body, settings)
    display_name = (message.sender_name or "").strip() or "Unknown sender"
    safe_name = message_text_for_log(display_name, settings)
    redacted_sender_name = json.loads(safe_name)
    redacted_text = json.loads(safe_text)
    occurred_at = (
        datetime.fromtimestamp(message.timestamp_ms / 1000.0, tz=timezone.utc)
        if message.timestamp_ms is not None
        else datetime.now(timezone.utc)
    )
    db_duplicate = False
    if settings.supabase_configured:
        inserted = persist_message(
            message,
            settings,
            redacted_sender_name=redacted_sender_name,
            redacted_text=redacted_text,
            occurred_at=occurred_at,
        )
        db_duplicate = not inserted
    receipt = receipts.record(
        message_reference(message), tenant_id=message.tenant_id,
        room_id=message.room_id, redacted_sender_name=redacted_sender_name,
        redacted_text=redacted_text,
        sender_kind="ai" if message.sender in settings.switch_ai_senders else "message",
        thread_ref=message_reference(message.model_copy(update={"message_id": message.thread_id}))
        if message.thread_id else None,
    )
    if db_duplicate and not receipt.duplicate:
        receipt = receipt.model_copy(update={"duplicate": True})
    logger.info("Switch message received ref=%s duplicate=%s", receipt.message_ref, receipt.duplicate)
    if settings.log_discord_messages:
        logger.info("Discord message ref=%s sender=%s text=%s", receipt.message_ref, safe_name, safe_text)
    return receipt


def get_recent_messages(settings: Settings, receipts: MessageReceiptRepository) -> list[MessagePreview]:
    if settings.switch_tenant_id is None or not settings.allowed_switch_rooms:
        raise HTTPException(503, "Switch ingestion is not configured")
    return receipts.recent(settings.switch_tenant_id, settings.allowed_switch_rooms)


def get_message_status(settings: Settings, receipts: MessageReceiptRepository) -> MessageStatus:
    recent = get_recent_messages(settings, receipts)
    latest = recent[0] if recent else None
    return MessageStatus(
        sender_name=latest.sender_name if latest else None,
        message=latest.text if latest else None,
    )
