from __future__ import annotations

import asyncio
from collections import defaultdict
import grpc
import time
import wave
import io

from storage_worker.config import Settings
from storage_worker.events import Event, now_ms
from storage_worker.kafka_events import KafkaEventProducer
from storage_worker.s3 import S3Uploader

import audio_pb2
import audio_pb2_grpc
import logging

from storage_worker.logger import configure_logging

logger = logging.getLogger(__name__)
configure_logging()
# ----------------------------
# PCM → WAV
# ----------------------------
def pcm_to_wav_bytes(pcm_bytes: bytes, sample_rate: int = 16000) -> bytes:
    buffer = io.BytesIO()

    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)

    return buffer.getvalue()


# ----------------------------
# SERVICE
# ----------------------------
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

        # 🧠 S3 async queue
        self.s3_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.s3_worker_task: asyncio.Task | None = None

        self.kafka_queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self.kafka_worker_task: asyncio.Task | None = None

    # ----------------------------
    # START
    # ----------------------------
    async def start(self) -> None:
        logger.info("starting_kafka")
        await self.kafka.start()

        logger.info("starting_s3_worker")
        self.s3_worker_task = asyncio.create_task(self._s3_worker())
        logger.info("starting_kafka_worker")
        self.kafka_worker_task = asyncio.create_task(self._kafka_worker())
        logger.info("service_ready")

    # ----------------------------
    # STOP
    # ----------------------------
    async def stop(self) -> None:
        await self.kafka.stop()

        logger.info("stopping_s3_worker")
        await self.s3_queue.put(None)

        if self.s3_worker_task:
            await self.s3_worker_task

        logger.info("stopping_kafka_worker")
        await self.kafka_queue.put(None)

        if self.kafka_worker_task:
            await self.kafka_worker_task

    # ----------------------------
    # BACKGROUND S3 WORKER
    # ----------------------------
    async def _s3_worker(self):
        logger.info("🟢 s3_worker_started")

        while True:
            item = await self.s3_queue.get()

            if item is None:
                break

            key, body = item
            started = time.perf_counter()
            try:
                self.s3.put_object(
                    key=key,
                    body=body,
                )
                elapsed = time.perf_counter() - started

                logger.info(
                    "s3_upload_complete key=%s bytes=%s duration_ms=%.1f",
                    key,
                    len(body),
                    elapsed * 1000,
                )
            except Exception as e:
                logger.exception("s3_upload_failed")

            self.s3_queue.task_done()

        logger.info("s3_worker_stopped")

    async def _kafka_worker(self):
        logger.info("🟢  kafka_worker_started")

        while True:
            event = await self.kafka_queue.get()

            if event is None:
                break

            try:
                await self.kafka.send(event)
            except Exception as e:
                logger.exception("kafka_error")

            self.kafka_queue.task_done()
        logger.info("kafka_worker_stopped")

    

    # ----------------------------
    # gRPC STREAM
    # ----------------------------
    async def StreamAudio(self, request_iterator, context):

        received_chunks = 0
        received_bytes = 0

        session_id = None

        async for chunk in request_iterator:

            session_id = chunk.session_id

            if session_id not in self.session_chunks:
                self.session_chunks[session_id] = 0
                logger.info(
                    "🟢 session started session_id=%s",
                    session_id,
                )

            self.session_chunks[session_id] += 1
            chunk_id = self.session_chunks[session_id]

            received_chunks += 1
            received_bytes += len(chunk.audio)

            # ----------------------------
            # WAV conversion
            # ----------------------------
            wav_data = pcm_to_wav_bytes(chunk.audio, chunk.sample_rate)

            s3_key = f"audio/{session_id}/{chunk_id:06d}.wav"

            # ----------------------------
            # 🚀 ASYNC S3 (NO BLOCKING)
            # ----------------------------
            await self.s3_queue.put((s3_key, wav_data))

            logger.info(
                "📦 queued_s3_upload "
                "session_id=%s chunk_id=%s bytes=%s",
                session_id,
                chunk_id,
                len(chunk.audio),
            )

            kafka_started = time.perf_counter()
            event = Event(
                type="audio_chunk_saved",
                session_id=session_id,
                timestamp_ms=now_ms(),
                payload={
                    "s3_key": s3_key,
                    "size_bytes": len(chunk.audio),
                    "chunk_id": chunk_id,
                },
            )
            await self.kafka_queue.put(event)

            logger.info(
                "📦 sending_kafka_event "
                "session_id=%s chunk_id=%s duration_ms=%.3f",
                session_id,
                chunk_id,
            (time.perf_counter() - kafka_started)*1000,
            )

            if chunk.is_end:
                self.session_chunks.pop(session_id, None)
                logger.info(
                    "🏁 session ended session_id=%s",
                    session_id,
                )

        return audio_pb2.StreamAck(
            session_id=session_id or "",
            received_chunks=received_chunks,
            received_bytes=received_bytes,
            s3_key=f"audio/{session_id}/",
        )


# ----------------------------
# SERVER
# ----------------------------
async def serve() -> None:
    settings = Settings()
    service = AudioIngestionService(settings)

    logger.info("🚀 worker_booting")
    server = grpc.aio.server()
    audio_pb2_grpc.add_AudioIngestionServicer_to_server(service, server)

    server.add_insecure_port(f"{settings.grpc_host}:{settings.grpc_port}")

    await server.start()

    logger.info("gRPC server started")
    await service.start()

    try:
        await server.wait_for_termination()
    finally:
        await service.stop()


if __name__ == "__main__":
    asyncio.run(serve())