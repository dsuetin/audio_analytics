from __future__ import annotations

import asyncio
import os
import time
import uuid

import grpc

from storage_worker import audio_pb2, audio_pb2_grpc


async def send_file(path: str, grpc_addr: str = "localhost:50051", sample_rate: int = 16000) -> None:
    session_id = str(uuid.uuid4())
    sequence = 0

    async def request_iter():
        nonlocal sequence
        with open(path, "rb") as f:
            while True:
                data = f.read(2400 * 2)  # ~150 ms of int16 mono at 16kHz
                if not data:
                    break

                sequence += 1
                yield audio_pb2.AudioChunk(
                    session_id=session_id,
                    sequence=sequence,
                    audio=data,
                    is_begin=(sequence == 1),
                    is_end=False,
                    sample_rate=sample_rate,
                    timestamp_ms=int(time.time() * 1000),
                    encoding="pcm_s16le",
                )

        yield audio_pb2.AudioChunk(
            session_id=session_id,
            sequence=sequence + 1,
            audio=b"",
            is_begin=False,
            is_end=True,
            sample_rate=sample_rate,
            timestamp_ms=int(time.time() * 1000),
            encoding="pcm_s16le",
        )

    async with grpc.aio.insecure_channel(grpc_addr) as channel:
        stub = audio_pb2_grpc.AudioIngestionStub(channel)
        ack = await stub.StreamAudio(request_iter())
        print(ack)


if __name__ == "__main__":
    audio_path = os.environ.get("AUDIO_PATH", "sample.raw")
    asyncio.run(send_file(audio_path))
