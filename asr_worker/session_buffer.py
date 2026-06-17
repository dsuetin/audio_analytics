from collections import defaultdict
import asyncio


class SessionBuffer:
    def __init__(self):
        self.buffers = defaultdict(list)
        self.locks = defaultdict(asyncio.Lock)

    async def add(self, session_id: str, chunk: bytes):
        async with self.locks[session_id]:
            self.buffers[session_id].append(chunk)

    async def pop_all(self, session_id: str):
        async with self.locks[session_id]:
            data = self.buffers[session_id]
            self.buffers[session_id] = []
            return data