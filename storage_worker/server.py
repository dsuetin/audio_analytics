from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import grpc

from storage_worker.config import Settings
from storage_worker.events import Event, now_ms
from storage_worker.kafka_events import KafkaEventProducer
from storage_worker.s3_multipart import S3MultipartUploader
from storage_worker.session_store import SessionState, SessionStore

import audio_pb2
import audio_pb2_grpc


def utc_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


class AudioIngestionService(audio_pb2_grpc.AudioIngestionServicer):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.sessions = SessionStore()

        self.s3 = S3MultipartUploader(
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
        print("🛑 Stopping Kafka...", flush=True)
        await self.kafka.stop()

    async def _begin_session(self, session_id: str, sample_rate: int) -> SessionState:
        s3_key = f"audio/{session_id}.raw"
        upload_id = self.s3.create_multipart_upload(s3_key)

        state = SessionState(
            session_id=session_id,
            s3_key=s3_key,
            upload_id=upload_id,
            started=True,
        )

        self.sessions.add(state)

        await self.kafka.send(
            Event(
                type="session_started",
                session_id=session_id,
                timestamp_ms=utc_ms(),
                payload={
                    "s3_key": s3_key,
                    "sample_rate": sample_rate,
                    "upload_id": upload_id,
                },
            )
        )

        print(f"🟢 session started: {session_id}", flush=True)
        return state

    async def _flush_part(self, state: SessionState) -> None:
        if not state.buffer:
            return

        body = bytes(state.buffer)

        etag = self.s3.upload_part(
            key=state.s3_key,
            upload_id=state.upload_id,
            part_number=state.part_number,
            body=body,
        )

        state.parts.append({"ETag": etag, "PartNumber": state.part_number})

        await self.kafka.send(
            Event(
                type="part_uploaded",
                session_id=state.session_id,
                timestamp_ms=utc_ms(),
                payload={
                    "part_number": state.part_number,
                    "size_bytes": len(body),
                },
            )
        )

        print(f"📦 part uploaded: {state.part_number}", flush=True)

        state.part_number += 1
        state.buffer.clear()

    async def _finalize_session(self, state: SessionState) -> None:
        try:
            if state.buffer:
                await self._flush_part(state)

            self.s3.complete_upload(state.s3_key, state.upload_id, state.parts)

            await self.kafka.send(
                Event(
                    type="session_completed",
                    session_id=state.session_id,
                    timestamp_ms=utc_ms(),
                    payload={
                        "s3_key": state.s3_key,
                        "parts": len(state.parts),
                    },
                )
            )

            print(f"🏁 session completed: {state.session_id}", flush=True)

        except Exception as exc:
            self.s3.abort_upload(state.s3_key, state.upload_id)

            await self.kafka.send(
                Event(
                    type="session_failed",
                    session_id=state.session_id,
                    timestamp_ms=utc_ms(),
                    payload={"error": str(exc)},
                )
            )

            print(f"❌ session failed: {exc}", flush=True)
            raise

        finally:
            self.sessions.remove(state.session_id)

    async def StreamAudio(self, request_iterator, context):
        active_state: SessionState | None = None
        received_chunks = 0
        received_bytes = 0
        final_s3_key = ""

        async for chunk in request_iterator:

            session_id, created = self.sessions.get_or_create(
                chunk.session_id or None
            )

            if chunk.is_begin or created or active_state is None:
                active_state = await self._begin_session(
                    session_id,
                    chunk.sample_rate,
                )

            if active_state is None:
                await context.abort(
                    grpc.StatusCode.FAILED_PRECONDITION,
                    "Session not initialized",
                )

            active_state.buffer.extend(chunk.audio)
            active_state.received_chunks += 1
            active_state.received_bytes += len(chunk.audio)

            received_chunks += 1
            received_bytes += len(chunk.audio)

            if len(active_state.buffer) >= self.settings.min_part_size_bytes:
                await self._flush_part(active_state)

            if chunk.is_end:
                await self._finalize_session(active_state)
                final_s3_key = active_state.s3_key
                active_state = None

        return audio_pb2.StreamAck(
            session_id="",
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

    print("🟢 gRPC server started", flush=True)

    # START KAFKA AFTER GRPC IS UP (CRITICAL FIX)
    await service.start()

    await service.kafka.send(
        Event(
            type="debug",
            session_id="boot",
            timestamp_ms=now_ms(),
            payload={"msg": "worker ready"},
        )
    )

    try:
        await server.wait_for_termination()
    finally:
        await service.stop()


if __name__ == "__main__":
    asyncio.run(serve())