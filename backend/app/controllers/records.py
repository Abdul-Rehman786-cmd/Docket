from fastapi import APIRouter, Depends
from app.core.security import require_admin_token
from app.models.records import (
    Commitment,
    DatabaseStatus,
    Decision,
    MemoryRecord,
    StoredMessage,
)
from app.services.record_service import (
    get_commitments,
    get_database_status,
    get_decisions,
    get_memories,
    get_stored_messages,
)

router = APIRouter(prefix="/api/v1", tags=["records"], dependencies=[Depends(require_admin_token)])


@router.get("/supabase/status", response_model=DatabaseStatus)
async def supabase_status() -> dict[str, str]:
    return await get_database_status()


@router.get("/commitments", response_model=list[Commitment])
async def list_commitments() -> list[dict]:
    return await get_commitments()


@router.get("/decisions", response_model=list[Decision])
async def list_decisions() -> list[dict]:
    return await get_decisions()


@router.get("/memories", response_model=list[MemoryRecord])
async def list_memories() -> list[dict]:
    return await get_memories()


@router.get("/messages", response_model=list[StoredMessage])
async def list_stored_messages() -> list[dict]:
    return await get_stored_messages()
