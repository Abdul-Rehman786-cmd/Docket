"""Private listener adapter ingress; viewer/admin credentials alone cannot forge consent."""

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.core.config import get_settings
from app.core.security import require_admin_token, require_ingest_token
from app.models.document_attachments import AttachmentMessage, AttachmentReceipt, AttachmentStatus

router=APIRouter(tags=["Switch document attachments"])


def require_document_adapter(x_document_adapter_token: str | None=Header(default=None)):
    settings=get_settings()
    expected=settings.switch_document_adapter_token.get_secret_value()
    if (not settings.switch_document_attachments_enabled or not expected
        or expected in (settings.switch_ingest_token.get_secret_value(),settings.admin_api_token)):
        raise HTTPException(503,"document_attachment_adapter_not_configured")
    if not x_document_adapter_token or not secrets.compare_digest(expected.encode(),x_document_adapter_token.encode()):
        raise HTTPException(401,"Unauthorized")


@router.post("/messages/document-attachments",response_model=AttachmentReceipt,
             dependencies=[Depends(require_ingest_token),Depends(require_document_adapter)])
async def receive_document_event(message: AttachmentMessage, request: Request):
    return await request.app.state.document_attachments.accept(message,get_settings())


@router.get("/api/v1/document-attachments/{request_ref}",response_model=AttachmentStatus,
            dependencies=[Depends(require_admin_token)])
async def attachment_status(request_ref: str, request: Request):
    return await request.app.state.document_attachments.status(request_ref,get_settings())
