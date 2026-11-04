import secrets
import logging
from fastapi import Header, HTTPException, status
from app.core.config import get_settings


def require_admin_token(x_admin_token: str | None = Header(default=None)) -> None:
    """Local/server-side admin access only; NEVER embed this token in mobile."""
    expected = get_settings().admin_api_token
    if not expected or not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        logging.getLogger("team_memory").debug("Admin authentication rejected")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


def require_ingest_token(x_ingest_token: str | None = Header(default=None)) -> None:
    """A dedicated server-to-server credential; grants no access to record reads."""
    expected = get_settings().switch_ingest_token.get_secret_value()
    if not expected or not x_ingest_token or not secrets.compare_digest(
        x_ingest_token.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
