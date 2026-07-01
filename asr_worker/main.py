import asyncio
import logging
import os
import sys
import time
from collections import defaultdict

from asr_worker.consumer import KafkaConsumerWrapper
from asr_worker.producer import KafkaProducerWrapper
from asr_worker.repo import TranscriptRepository
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
        self.asr_events = asyncio.Queue()
        self.asr = TritonASRClient(self.asr_events)
        self.buffer = SessionBuffer()

        # защита от гонок внутри одной сессии
        self.session_locks = defaultdict(asyncio.Lock)

        # активные task’и на сессии
        self.session_tasks: dict[str, asyncio.Task] = {}

        self.repo = TranscriptRepository(
            dsn=os.getenv(
                "POSTGRES_DSN",
                "postgresql://speech:speech@postgres:5432/speech_db"
            )
        )

        self.producer = KafkaProducerWrapper(
            bootstrap="redpanda:9092",
        )
        # self.asr_events = asyncio.Queue()

    # ----------------------------
    # async S3 wrapper (non-blocking)
    # ----------------------------
    async def get_s3_object(self, key: str) -> bytes:
        return await asyncio.to_thread(
            self.s3.get_object,
            key,
        )
    
    async def asr_event_worker(self):
        while True:

            event = await self.asr_events.get()
            # print("Processing ASR event:", event)

            try:
                await self.producer.send(
                    "asr_transcripts",
                    {
                        "session_id": event["session_id"],
                        "chunk_id": event["chunk_id"],
                        "text": event["text"],
                        "is_final": event["is_final"],
                    },
                )

                await self.repo.save(
                    session_id=event["session_id"],
                    text=event["text"],
                    is_final=event["is_final"],
                )

            except Exception:
                logger.exception("failed processing asr event")
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
        logger.info(
            "schedule session=%s existing_task=%s",
            session_id,
            self.session_tasks.get(session_id),
        )

        task = self.session_tasks.get(session_id)

        if task is None or task.done():
            logger.info("create task session=%s", session_id)
            task = asyncio.create_task(self.process_session(session_id))
            self.session_tasks[session_id] = task

    # ----------------------------
    # core session processing
    # ----------------------------
    async def process_session(self, session_id: str):
        logger.info("process started %s", session_id)

        async with self.session_locks[session_id]:

            # streaming chunks
            chunk_id = 0
            while True:

                if await self.buffer.is_end_ready(session_id):

                    # print("final!!!!!!!!!!!!!")
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

                    self.buffer.buf.pop(session_id, None)
                    self.buffer.locks.pop(session_id, None)

                    # cleanup ASR state
                    self.asr.started.discard(session_id)
                    self.asr.seq_map.pop(session_id, None)

                    # cleanup session state
                    self.session_tasks.pop(session_id, None)
                    self.session_locks.pop(session_id, None)

                    logger.info("session closed session=%s", session_id)
            

                chunk = await self.buffer.pop_if_ready(
                    session_id,
                    min_ms=160,
                )
                chunk_id += 1

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

                



async def main():
    worker = ASRWorker()

    consumer = KafkaConsumerWrapper(
        bootstrap="redpanda:9092",
        topic="audio_events",
    )

    await consumer.start()
    await worker.repo.start()
    await worker.producer.start()
    logger.info("producer started")
    
    event_worker_task = asyncio.create_task(
        worker.asr_event_worker()
    )
    try:
        await consumer.run(worker.handle_event)
    finally:
        event_worker_task.cancel()
        await consumer.stop()
        await worker.repo.stop()
        await worker.producer.stop()


if __name__ == "__main__":
    asyncio.run(main())