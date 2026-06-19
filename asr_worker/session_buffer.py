import asyncio
from collections import defaultdict
from dataclasses import dataclass


@dataclass
class BufferState:
    data: bytearray
    is_end: bool = False
    cursor: int = 0


class SessionBuffer:
    def __init__(self, sample_rate: int = 16000, sample_width: int = 2):
        self.buf = defaultdict(lambda: BufferState(bytearray()))
        self.locks = defaultdict(asyncio.Lock)

        self.bytes_per_ms = (sample_rate * sample_width) / 1000  # 32 bytes/ms

    async def add(self, session_id: str, data: bytes, is_end: bool = False):
        async with self.locks[session_id]:
            state = self.buf[session_id]
            state.data.extend(data)
            state.is_end = state.is_end or is_end

    async def pop_if_ready(self, session_id: str, min_ms: int = 160) -> bytes | None:
        async with self.locks[session_id]:
            state = self.buf[session_id]

            available = len(state.data) - state.cursor
            target_bytes = int(min_ms * self.bytes_per_ms)

            # важно: всегда выравниваем по int16
            target_bytes = (target_bytes // 2) * 2

            if available < target_bytes:
                return None

            chunk = bytes(state.data[state.cursor: state.cursor + target_bytes])
            state.cursor += target_bytes
            return chunk

    async def is_end_ready(self, session_id: str) -> bool:
        async with self.locks[session_id]:
            state = self.buf[session_id]
            return state.is_end

    async def pop_all(self, session_id: str) -> bytes:
        async with self.locks[session_id]:
            state = self.buf[session_id]

            remaining = bytes(state.data[state.cursor:])

            # cleanup
            del self.buf[session_id]

            return remaining