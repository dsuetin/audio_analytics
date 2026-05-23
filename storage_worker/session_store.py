from __future__ import annotations

from dataclasses import dataclass, field
import uuid


@dataclass
class SessionState:
    session_id: str
    s3_key: str
    upload_id: str
    buffer: bytearray = field(default_factory=bytearray)
    part_number: int = 1
    parts: list[dict] = field(default_factory=list)
    received_chunks: int = 0
    received_bytes: int = 0
    started: bool = False
    finalized: bool = False


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}

    def get_or_create(self, session_id: str | None) -> tuple[str, bool]:
        if session_id and session_id in self._sessions:
            return session_id, False
        new_session_id = session_id or str(uuid.uuid4())
        return new_session_id, True

    def add(self, state: SessionState) -> None:
        self._sessions[state.session_id] = state

    def get(self, session_id: str) -> SessionState:
        return self._sessions[session_id]

    def remove(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
