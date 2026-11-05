from pathlib import Path
import re

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from app.services.viewer_service import serve_viewer

router = APIRouter(include_in_schema=False)
VIEWS = Path(__file__).resolve().parents[1] / "views"
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
}


def viewer_headers(request: Request):
    headers = dict(HEADERS)
    # Explicit same-host ws/wss source for browsers that don't map CSP 'self'
    # to WebSockets. Validate before inserting a Host-derived value into CSP.
    host = request.url.netloc
    if re.fullmatch(r"(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(?::[0-9]{1,5})?", host):
        scheme = "wss" if request.url.scheme == "https" else "ws"
        headers["Content-Security-Policy"] = headers["Content-Security-Policy"].replace(
            "connect-src 'self';", f"connect-src 'self' {scheme}://{host};"
        )
    return headers


@router.get("/")
def message_viewer(request: Request) -> FileResponse:
    """Public page shell; private data requires authentication through the API."""
    return FileResponse(VIEWS / "messages.html", media_type="text/html", headers=viewer_headers(request))


@router.get("/viewer.css")
def viewer_styles(request: Request) -> FileResponse:
    return FileResponse(VIEWS / "messages.css", media_type="text/css", headers=viewer_headers(request))


@router.get("/viewer.js")
def viewer_script(request: Request) -> FileResponse:
    return FileResponse(VIEWS / "messages.js", media_type="text/javascript", headers=viewer_headers(request))


@router.websocket("/viewer/ws")
async def viewer_socket(websocket: WebSocket):
    await serve_viewer(websocket)


@router.get("/viewer/env")
def get_env_variables(request: Request) -> JSONResponse:
    headers = viewer_headers(request)
    headers["Cache-Control"] = "no-store, private"
    return JSONResponse(status_code=410,content={"detail":"Environment configuration is local-only"},headers=headers)


@router.post("/viewer/env")
def update_env_variables(request: Request) -> JSONResponse:
    headers = viewer_headers(request)
    headers["Cache-Control"] = "no-store, private"
    return JSONResponse(status_code=410,content={"detail":"Environment configuration is local-only"},headers=headers)
