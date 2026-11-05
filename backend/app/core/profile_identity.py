"""Profile HTTP access requires a verified individual identity integration."""

from fastapi import HTTPException, Request
from app.models.commitments import Actor


async def resolve_profile_actor(request: Request) -> Actor:
    # Shared admin/ingest tokens and caller-provided identity headers are not people.
    raise HTTPException(503, "Individual-user authentication is not configured for person profiles")
