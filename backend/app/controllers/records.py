from fastapi import APIRouter, Depends
from app.core.security import require_admin_token
from app.models.records import Commitment, Decision
from app.services.record_service import get_commitments, get_decisions

router = APIRouter(prefix="/api/v1", tags=["records"], dependencies=[Depends(require_admin_token)])


@router.get("/commitments", response_model=list[Commitment])
def list_commitments() -> list[dict]:
    return get_commitments()


@router.get("/decisions", response_model=list[Decision])
def list_decisions() -> list[dict]:
    return get_decisions()
