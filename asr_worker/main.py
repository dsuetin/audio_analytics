import asyncio
from email.mime import audio
import logging
import sys
import time
import os

from asr_worker.consumer import KafkaConsumerWrapper
from asr_worker.s3_client import S3Client
from asr_worker.session_buffer import SessionBuffer
from asr_worker.triton_client import TritonASRClient

logger = logging.getLogger(__name__)

def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s "
            "%(levelname)s "
            "%(name)s "
            "%(message)s"
        ),
        stream=sys.stdout,
    )

configure_logging()

import io
import wave

def wav_to_pcm(wav_bytes: bytes) -> bytes:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        return wf.readframes(wf.getnframes())

class ASRWorker:
    def __init__(self):
        self.s3 = S3Client(
            endpoint="http://minio:9000",
            key="minioadmin",
            secret="minioadmin",
            bucket="audio-sessions",
        )

        self.asr = TritonASRClient()
        self.sessions = set()
        self.buffer = SessionBuffer()


    async def save_session_to_file(self, session_id: str):
        chunks = await self.buffer.pop_all(session_id)

        if not chunks:
            return

        os.makedirs("/app/asr_worker/debug", exist_ok=True)

        output_path = f"/app/asr_worker/debug/{session_id}.wav"

        merged_pcm = b"".join(chunks)

        with wave.open(output_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(merged_pcm)

        logger.info(
            "💾 session_saved session_id=%s path=%s chunks=%s bytes=%s",
            session_id,
            output_path,
            len(chunks),
            len(merged_pcm),
        )

    async def handle_event(self, event: dict):
        if event.get("type") != "audio_chunk_saved":
            return

        session_id = event["session_id"]
        chunk_id = event["payload"]["chunk_id"]
        s3_key = event["payload"]["s3_key"]
        is_end = event["payload"].get("is_end", False)
        started = time.perf_counter()

        audio = self.s3.get_object(s3_key)
        pcm = wav_to_pcm(audio)
        elapsed_ms = (time.perf_counter() - started) * 1000

        logger.info(
            "📥 s3_read "
            "session_id=%s chunk_id=%s bytes=%s duration_ms=%.2f",
            session_id,
            chunk_id,
            len(audio),
            elapsed_ms,
        )

        await self.buffer.add(
            session_id=session_id,
            chunk=pcm,
        )
        logger.info(
            "📦 buffered session_id=%s chunk_id=%s is_end=%s",
            session_id,
            chunk_id,
            is_end,
        )

        # временно сохраняем для дебага
        if is_end:
            await self.save_session_to_file(session_id)

async def main():
    worker = ASRWorker()

    consumer = KafkaConsumerWrapper(
        bootstrap="redpanda:9092",
        topic="audio_events",
    )

    await consumer.start()
    print("INNER TYPE:", type(consumer._consumer))
    await consumer.run(worker.handle_event)


if __name__ == "__main__":
    asyncio.run(main())