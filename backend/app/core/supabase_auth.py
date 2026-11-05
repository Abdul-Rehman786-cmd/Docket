"""Backend API-key classification. Never log or return credential contents."""

import base64
import json
from fastapi import HTTPException


def backend_key_type(key: str) -> str:
    if key.startswith("sb_secret_"):
        return "secret"
    if key.startswith("sb_publishable_"):
        return "public"
    if key.count(".") == 2:
        try:
            part = key.split(".")[1]
            payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
            return "legacy_service_role" if payload.get("role") == "service_role" else "public"
        except (ValueError,TypeError,AttributeError):
            return "invalid"
    return "unknown"  # The server verifies opaque values; useful for mock transports.


def backend_headers(key: str) -> dict[str,str]:
    kind = backend_key_type(key)
    if kind in ("public","invalid"):
        raise HTTPException(503,"SUPABASE_SERVICE_ROLE_KEY must be a backend secret or service-role key")
    headers = {"apikey":key}
    # New opaque secret keys are not JWTs. The hosted gateway supplies the
    # service-role authorization from apikey; do not treat them as bearer JWTs.
    if kind != "secret":
        headers["Authorization"] = f"Bearer {key}"
    return headers
