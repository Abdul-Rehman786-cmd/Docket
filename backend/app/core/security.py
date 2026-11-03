import secrets
from fastapi import Header, HTTPException, status
from app.core.config import get_settings


def require_admin_token(x_admin_token: str | None = Header(default=None)) -> None:
    """Local/server-side admin access only; NEVER embed this token in mobile."""
    expected = get_settings().admin_api_token
    if not expected or not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
