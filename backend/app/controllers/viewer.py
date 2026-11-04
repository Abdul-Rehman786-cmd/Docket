from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

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


@router.get("/")
def message_viewer() -> FileResponse:
    """Public page shell; private data requires authentication through the API."""
    return FileResponse(VIEWS / "messages.html", media_type="text/html", headers=HEADERS)


@router.get("/viewer.css")
def viewer_styles() -> FileResponse:
    return FileResponse(VIEWS / "messages.css", media_type="text/css", headers=HEADERS)


@router.get("/viewer.js")
def viewer_script() -> FileResponse:
    return FileResponse(VIEWS / "messages.js", media_type="text/javascript", headers=HEADERS)
