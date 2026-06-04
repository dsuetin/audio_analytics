from __future__ import annotations

import asyncio
import grpc

from storage_worker.config import Settings
from storage_worker.events import Event, now_ms
from storage_worker.kafka_events import KafkaEventProducer
from storage_worker.s3 import S3Uploader

import audio_pb2
import audio_pb2_grpc


class SessionState:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.buffer = bytearray()

        self.received_chunks = 0
        self.received_bytes = 0


class AudioIngestionService(audio_pb2_grpc.AudioIngestionServicer):
    def __init__(self, settings: Settings):
        self.settings = settings

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

    def _make_s3_key(self, session_id: str) -> str:
        return f"audio/{session_id}.raw"

    async def StreamAudio(self, request_iterator, context):
        active_state: SessionState | None = None

        received_chunks = 0
        received_bytes = 0
        final_s3_key = ""

        async for chunk in request_iterator:
            session_id = chunk.session_id

            if chunk.is_begin or active_state is None:
                active_state = SessionState(session_id)
                print(f"🟢 session started: {session_id}", flush=True)

            active_state.buffer.extend(chunk.audio)

            active_state.received_chunks += 1
            active_state.received_bytes += len(chunk.audio)

            received_chunks += 1
            received_bytes += len(chunk.audio)

            if chunk.is_end:
                s3_key = self._make_s3_key(session_id)

                # 1. SAVE TO S3 (single request)
                self.s3.put_object(
                    key=s3_key,
                    body=bytes(active_state.buffer),
                )

                # 2. KAFKA EVENT (only business event)
                await self.kafka.send(
                    Event(
                        type="session_completed",
                        session_id=session_id,
                        timestamp_ms=now_ms(),
                        payload={
                            "s3_key": s3_key,
                            "size_bytes": len(active_state.buffer),
                        },
                    )
                )

                print(f"🏁 session completed: {session_id}", flush=True)

                final_s3_key = s3_key
                active_state = None

        return audio_pb2.StreamAck(
            session_id=session_id,
            received_chunks=received_chunks,
            received_bytes=received_bytes,
            s3_key=final_s3_key,
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