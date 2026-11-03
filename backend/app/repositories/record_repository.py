import httpx
from fastapi import HTTPException
from app.core.config import get_settings


def list_records(table: str) -> list[dict]:
    """Read records via Supabase REST. Only invoked by protected backend endpoints."""
    if table not in ("commitments", "decisions"):
        raise ValueError("Unknown table")
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise HTTPException(503, "Database is not configured")
    try:
        response = httpx.get(
            f"{settings.supabase_url.rstrip('/')}/rest/v1/{table}",
            params={"select": "*", "order": "created_at.desc", "limit": "50"},
            headers={
                "apikey": settings.supabase_service_role_key,
                "Authorization": f"Bearer {settings.supabase_service_role_key}",
            },
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise ValueError("Unexpected database response")
        return data
    except (httpx.HTTPError, ValueError) as exc:
        # Do not expose database URLs, API responses, or credentials to the client.
        raise HTTPException(503, "Database request failed") from exc
