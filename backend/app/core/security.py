import secrets
import logging
from fastapi import Header, HTTPException, status
from app.core.config import get_settings
from urllib.parse import urlsplit


def ingest_token_matches(token, settings) -> bool:
    expected = settings.switch_ingest_token.get_secret_value()
    return bool(expected and token and secrets.compare_digest(
        token.encode("utf-8"), expected.encode("utf-8")
    ))


def viewer_origin_matches(websocket) -> bool:
    try:
        origin = urlsplit(websocket.headers.get("origin", ""))
    except ValueError:
        return False
    expected_scheme = "https" if websocket.url.scheme == "wss" else "http"
    return bool(
        origin.scheme == expected_scheme and origin.netloc == websocket.url.netloc
        and not origin.username and not origin.password
        and origin.path in ("", "/") and not origin.query and not origin.fragment
    )


def require_admin_token(x_admin_token: str | None = Header(default=None)) -> None:
    """Local/server-side admin access only; NEVER embed this token in mobile."""
    expected = get_settings().admin_api_token
    if not expected or not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        logging.getLogger("team_memory").debug("Admin authentication rejected")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


def require_ingest_token(x_ingest_token: str | None = Header(default=None)) -> None:
    """A dedicated server-to-server credential; grants no access to record reads."""
    if not ingest_token_matches(x_ingest_token, get_settings()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
