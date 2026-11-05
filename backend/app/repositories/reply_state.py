"""Persisted watermark for Discord AI replies. Avoids reposting replies to replayed or older messages."""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class ReplyState:
    timestamp_ms: int | None = None
    message_id: str | None = None
    room_id: str | None = None
    replied_at: str | None = None

    @classmethod
    def load(cls, path: Path) -> "ReplyState":
        if not path.exists():
            return cls()
        try:
            content = path.read_text(encoding="utf-8")
            data: dict[str, str] = {}
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    data[key.strip()] = value.strip()
                elif line.isdigit() and "timestamp_ms" not in data:
                    data["timestamp_ms"] = line

            ts_raw = data.get("timestamp_ms")
            timestamp_ms = int(ts_raw) if ts_raw and ts_raw.isdigit() else None
            return cls(
                timestamp_ms=timestamp_ms,
                message_id=data.get("message_id") or None,
                room_id=data.get("room_id") or None,
                replied_at=data.get("replied_at") or None,
            )
        except Exception:
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Watermark of the latest Discord message that received an AI reply.",
            f"timestamp_ms={self.timestamp_ms if self.timestamp_ms is not None else ''}",
            f"replied_at={self.replied_at or datetime.now(timezone.utc).isoformat()}",
            f"message_id={self.message_id or ''}",
            f"room_id={self.room_id or ''}",
        ]
        temporary = path.with_suffix(".tmp")
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temporary.replace(path)

