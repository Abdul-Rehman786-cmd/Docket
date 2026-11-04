import json
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID, uuid4


@dataclass
class ListenerState:
    identity: str
    connection_id: str
    cursor: int | None = None
    generation: int | None = None

    @classmethod
    def load(cls, path: Path, identity: str) -> "ListenerState":
        if not path.exists():
            return cls(identity=identity, connection_id=str(uuid4()))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["identity"] != identity:
                return cls(identity=identity, connection_id=str(uuid4()))
            UUID(data["connection_id"])
            for field in ("cursor", "generation"):
                value = data.get(field)
                if value is not None and (type(value) is not int or value < 0):
                    raise ValueError("Invalid state")
            return cls(**data)
        except (KeyError, TypeError, ValueError):
            raise ValueError("Listener state is invalid; inspect the local state file before restarting.") from None

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self)), encoding="utf-8")
        temporary.replace(path)
