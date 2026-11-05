"""Read-only profile HTTP endpoints; database access stays in the repository."""

from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends, Query

from app.core.config import get_settings
from app.core.profile_identity import resolve_profile_actor
from app.models.commitments import Actor, Scope
from app.models.profiles import ProfilePage, ProfileDetail
from app.services.profile_service import read_profile

router = APIRouter(prefix='/api/v1/tenants/{tenant_id}/rooms/{room_id}/profiles', tags=['person profiles'])
Human = Annotated[Actor, Depends(resolve_profile_actor)]


@router.get('', response_model=ProfilePage)
async def list_profiles(tenant_id: UUID, room_id: UUID, actor: Human,
                        settings=Depends(get_settings),
                        limit: Annotated[int, Query(ge=1, le=100)] = 50, after: UUID | None = None):
    return await read_profile(settings, Scope(tenant_id=tenant_id, room_id=room_id), actor, limit=limit, after=after)


@router.get('/{profile_id}', response_model=ProfileDetail)
async def get_profile(tenant_id: UUID, room_id: UUID, profile_id: UUID, actor: Human, settings=Depends(get_settings)):
    return await read_profile(settings, Scope(tenant_id=tenant_id, room_id=room_id), actor, profile_id=profile_id)
