from fastapi import APIRouter, Depends, Request

from app.core.config import get_settings
from app.core.security import require_ingest_token
from app.models.ai import AIResponse, AskRequest
from app.services.ai_service import response_scope

router = APIRouter(prefix="/ai", tags=["server-only AI"], dependencies=[Depends(require_ingest_token)])


@router.post("/requests", response_model=AIResponse, status_code=202)
async def ask_codex(body: AskRequest, request: Request):
    return await request.app.state.ai_service.submit(body, get_settings(), request.app.state.message_receipts)


@router.get("/requests", response_model=list[AIResponse])
async def recent_answers(request: Request):
    return request.app.state.ai_service.repository.recent(response_scope(get_settings()))
