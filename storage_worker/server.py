from __future__ import annotations

import asyncio
from collections import defaultdict
import grpc
import time
import wave
import io
import asyncio
from storage_worker.config import Settings
from storage_worker.events import Event, now_ms
from storage_worker.kafka_events import KafkaEventProducer
from storage_worker.s3 import S3Uploader

import audio_pb2
import audio_pb2_grpc


def pcm_to_wav_bytes(pcm_bytes: bytes, sample_rate: int = 16000) -> bytes:
    buffer = io.BytesIO()

    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)        # mono
        wf.setsampwidth(2)        # int16 = 2 bytes
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)

    return buffer.getvalue()
class SessionState:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.buffer = bytearray()

        self.received_chunks = 0
        self.received_bytes = 0


class AudioIngestionService(audio_pb2_grpc.AudioIngestionServicer):
    def __init__(self, settings):
        self.settings = settings
        self.session_chunks = defaultdict(int)
        self.s3 = S3Uploader(
            endpoint_url=settings.s3_endpoint_url,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
            bucket=settings.s3_bucket,
        )
    
        self.kafka = KafkaEventProducer(
            bootstrap_servers=settings.kafka_bootstrap_servers,
            topic=settings.kafka_events_topic,
        )

    async def start(self) -> None:
        print("🚀 Starting Kafka...", flush=True)
        await self.kafka.start()
        print("✅ Kafka ready", flush=True)

    async def stop(self) -> None:
        await self.kafka.stop()


    async def StreamAudio(self, request_iterator, context):

        received_chunks = 0
        received_bytes = 0

        session_id = None
        now = time.perf_counter()
        async for chunk in request_iterator:

            session_id = chunk.session_id

            # init counter per session
            if session_id not in self.session_chunks:
                self.session_chunks[session_id] = 0
                print(f"🟢 session started: {session_id}", flush=True)

            self.session_chunks[session_id] += 1
            chunk_id = self.session_chunks[session_id]

            received_chunks += 1
            received_bytes += len(chunk.audio)

            s3_key = f"audio/{session_id}/{chunk_id:06d}.wav"

            wav_data = pcm_to_wav_bytes(chunk.audio, chunk.sample_rate)
            s3_started = time.perf_counter()
            self.s3.put_object(
                key=s3_key,
                body=wav_data,
            )

            print(
                f"💾 saving chunk "
                f"session={session_id} "
                f"chunk={chunk_id} "
                f"bytes={len(chunk.audio)}",
                f"time={time.perf_counter() - s3_started:.3f}s",
                flush=True
            )

            kafka_started = time.perf_counter()
            await self.kafka.send(
                Event(
                    type="audio_chunk_saved",
                    session_id=session_id,
                    timestamp_ms=now_ms(),
                    payload={
                        "s3_key": s3_key,
                        "size_bytes": len(chunk.audio),
                        "chunk_id": chunk_id,
                    },
                )
            )

            print(
                f"📨 sending Kafka event "
                f"session={session_id} "
                f"chunk={chunk_id} "
                f"time={time.perf_counter() - kafka_started:.3f}s",
                flush=True
            )

            if chunk.is_end:
                self.session_chunks.pop(session_id, None)
                print(f"🏁 session ended: {session_id}", flush=True)
                # break
            

        return audio_pb2.StreamAck(
            session_id=session_id or "",
            received_chunks=received_chunks,
            received_bytes=received_bytes,
            s3_key=f"audio/{session_id}/",
        )


async def serve() -> None:
    settings = Settings()
    service = AudioIngestionService(settings)

    print("🚀 WORKER BOOTING", flush=True)

    server = grpc.aio.server()
    audio_pb2_grpc.add_AudioIngestionServicer_to_server(service, server)

    server.add_insecure_port(f"{settings.grpc_host}:{settings.grpc_port}")

    await server.start()

    print("🟢🟢🟢 gRPC server started", flush=True)

    await service.start()

    try:
        await server.wait_for_termination()
    finally:
        await service.stop()


if __name__ == "__main__":
    asyncio.run(serve())