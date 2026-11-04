from fastapi import APIRouter, Depends
from app.core.security import require_admin_token
from app.models.records import Commitment, DatabaseStatus, Decision, StoredMessage
from app.services.record_service import (
    get_commitments,
    get_database_status,
    get_decisions,
    get_stored_messages,
)

router = APIRouter(prefix="/api/v1", tags=["records"], dependencies=[Depends(require_admin_token)])


@router.get("/supabase/status", response_model=DatabaseStatus)
def supabase_status() -> dict[str, str]:
    return get_database_status()


@router.get("/commitments", response_model=list[Commitment])
def list_commitments() -> list[dict]:
    return get_commitments()


@router.get("/decisions", response_model=list[Decision])
def list_decisions() -> list[dict]:
    return get_decisions()


@router.get("/messages", response_model=list[StoredMessage])
def list_stored_messages() -> list[dict]:
    return get_stored_messages()
