from fastapi import APIRouter, Depends, Request

from app.core.config import get_settings
from app.core.security import require_ingest_token
from app.models.messages import IncomingMessage, MessagePreview, MessageReceipt, MessageStatus
from app.services.message_service import get_message_status, get_recent_messages, receive_message

router = APIRouter(prefix="/messages", tags=["messages"], dependencies=[Depends(require_ingest_token)])


@router.post("", response_model=MessageReceipt)
async def ingest_message(message: IncomingMessage, request: Request) -> MessageReceipt:
    """Acknowledge delivery, retain a redacted preview, and trigger AI 4-way database evaluation."""
    settings = get_settings()
    receipt = receive_message(message, settings, request.app.state.message_receipts)
    await request.app.state.ai_service.evaluate_incoming_message(
        message, receipt, settings, request.app.state.message_receipts
    )
    return receipt


@router.get("/status", response_model=MessageStatus)
def message_status(request: Request) -> MessageStatus:
    """Latest sender display name and redacted message for the configured tenant and rooms."""
    return get_message_status(get_settings(), request.app.state.message_receipts)


@router.get("/recent", response_model=list[MessagePreview])
def recent_messages(request: Request) -> list[MessagePreview]:
    """Latest redacted previews for the configured tenant and room allow-list."""
    return get_recent_messages(get_settings(), request.app.state.message_receipts)
