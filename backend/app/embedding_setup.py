"""Download fixed public model assets. Never sends chat or credentials to a model host."""

import hashlib
import json
import httpx

from app.core.embedding_model import ASSETS, MODEL_DIR, REVISION, EmbeddingError, verify_assets


def main():
    with httpx.Client(trust_env=False, follow_redirects=True, timeout=120) as client:
        for name, expected in ASSETS.items():
            path = MODEL_DIR / name
            if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            part = path.with_suffix(path.suffix + ".part")
            digest = hashlib.sha256()
            with client.stream("GET", f"https://huggingface.co/Supabase/gte-small/resolve/{REVISION}/{name}") as response:
                response.raise_for_status()
                with part.open("wb") as stream:
                    for block in response.iter_bytes(1024 * 1024):
                        digest.update(block)
                        stream.write(block)
            if digest.hexdigest() != expected:
                raise EmbeddingError("model_checksum_failed")
            part.replace(path)
    verify_assets()
    print(json.dumps({"model_assets_verified": True, "revision": REVISION}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"error":"model_setup_failed"}')
        raise SystemExit(1) from None
