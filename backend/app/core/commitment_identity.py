"""Integration seam for a future verified human session provider, closed by default."""

from typing import Protocol

from fastapi import HTTPException, Request

from app.models.commitments import Actor
from app.core.feature_logging import feature_event


class ActorResolver(Protocol):
    async def resolve(self, request: Request) -> Actor: ...


async def resolve_commitment_actor(request: Request) -> Actor:
    # Never interpret the admin/ingest token, a body field, or a role header as a person.
    # An authentication integration must replace this dependency with verified stable IDs.
    feature_event('commitment','blocked',reason='human_auth_unavailable',status=503)
    raise HTTPException(503, "Individual-user authentication is not configured for commitment management")
