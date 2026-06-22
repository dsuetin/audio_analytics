import asyncio
import logging
import sys
import time
from collections import defaultdict

from asr_worker.consumer import KafkaConsumerWrapper
from asr_worker.s3_client import S3Client
from asr_worker.session_buffer import SessionBuffer
from asr_worker.triton_client import TritonASRClient


logger = logging.getLogger(__name__)


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


configure_logging()


def wav_to_pcm(wav_bytes: bytes) -> bytes:
    import io
    import wave

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
        self.buffer = SessionBuffer()

        # защита от гонок внутри одной сессии
        self.session_locks = defaultdict(asyncio.Lock)

        # активные task’и на сессии
        self.session_tasks: dict[str, asyncio.Task] = {}

    # ----------------------------
    # async S3 wrapper (non-blocking)
    # ----------------------------
    async def get_s3_object(self, key: str) -> bytes:
        return await asyncio.to_thread(
            self.s3.get_object,
            key,
        )

    # ----------------------------
    # Kafka handler
    # ----------------------------
    async def handle_event(self, event: dict):
        if event.get("type") != "audio_chunk_saved":
            return

        session_id = event["session_id"]
        payload = event["payload"]

        s3_key = payload["s3_key"]
        chunk_id = payload["chunk_id"]
        is_end = payload.get("is_end", False)

        started = time.perf_counter()

        # async S3 read (НЕ блокируем event loop)
        audio = await self.get_s3_object(s3_key)
        pcm = wav_to_pcm(audio)

        # buffer insert (ordering + reassembly)
        await self.buffer.add(
            session_id=session_id,
            chunk_id=chunk_id,
            data=pcm,
            is_end=is_end,
        )

        # dispatch session processing
        self._schedule_session(session_id)

        elapsed_ms = (time.perf_counter() - started) * 1000

        logger.info(
            "s3_read session=%s chunk=%s is_end=%s bytes=%s time_ms=%.2f",
            session_id,
            chunk_id,
            is_end,
            len(audio),
            elapsed_ms,
        )

    # ----------------------------
    # session scheduler
    # ----------------------------
    def _schedule_session(self, session_id: str):
        task = self.session_tasks.get(session_id)

        if task is None or task.done():
            task = asyncio.create_task(self.process_session(session_id))
            self.session_tasks[session_id] = task

    # ----------------------------
    # core session processing
    # ----------------------------
    async def process_session(self, session_id: str):
        async with self.session_locks[session_id]:

            # streaming chunks
            while True:
                chunk = await self.buffer.pop_if_ready(
                    session_id,
                    min_ms=160,
                )

                if chunk is None:
                    break

                logger.info(
                    "ASR stream session=%s bytes=%s",
                    session_id,
                    len(chunk),
                )

                await self.asr.send(
                    session_id,
                    chunk,
                    is_last=False,
                )

            # finalize
            if await self.buffer.is_end_ready(session_id):
                final = await self.buffer.pop_all(session_id)

                logger.info(
                    "ASR FINAL session=%s bytes=%s",
                    session_id,
                    len(final),
                )

                await self.asr.send(
                    session_id,
                    final,
                    is_last=True,
                )

                # cleanup buffer
                self.buffer.buf.pop(session_id, None)
                self.buffer.locks.pop(session_id, None)

                # cleanup ASR state
                self.asr.started.discard(session_id)
                self.asr.seq_map.pop(session_id, None)

                # cleanup session state
                self.session_tasks.pop(session_id, None)
                self.session_locks.pop(session_id, None)

                logger.info("session closed session=%s", session_id)


async def main():
    worker = ASRWorker()

    consumer = KafkaConsumerWrapper(
        bootstrap="redpanda:9092",
        topic="audio_events",
    )

    await consumer.start()

    try:
        await consumer.run(worker.handle_event)
    finally:
        await consumer.stop()


if __name__ == "__main__":
    asyncio.run(main())