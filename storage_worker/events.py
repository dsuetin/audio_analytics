from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone


@dataclass
class Event:
    type: str
    session_id: str
    timestamp_ms: int
    payload: dict

    def to_json_bytes(self) -> bytes:
        doc = {
            "type": self.type,
            "session_id": self.session_id,
            "timestamp_ms": self.timestamp_ms,
            "payload": self.payload,
        }
        return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)
