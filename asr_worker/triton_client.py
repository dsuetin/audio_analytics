import time
import asyncio

class TritonASRClient:
    def __init__(self):
        self.seq = {}

    async def start_session(self, session_id: str):
        self.seq[session_id] = 0

    async def send(self, session_id: str, audio: bytes):
        self.seq[session_id] += 1

        # MOCK latency
        await asyncio.sleep(0.05)

        # MOCK transcription
        text = f"[{session_id[:8]}] chunk {self.seq[session_id]} -> hello world"

        print("🧠 ASR:", text)
        return text