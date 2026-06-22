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

        # защита от параллельной обработки одной сессии
        self.session_locks = defaultdict(asyncio.Lock)

    async def handle_event(self, event: dict):
        if event.get("type") != "audio_chunk_saved":
            return

        session_id = event["session_id"]
        payload = event["payload"]

        s3_key = payload["s3_key"]
        chunk_id = payload["chunk_id"]
        is_end = payload.get("is_end", False)

        started = time.perf_counter()

        # ⚠️ S3 read (можно позже перевести в async)
        audio = self.s3.get_object(s3_key)
        pcm = wav_to_pcm(audio)

        # кладём в буфер с восстановлением порядка
        await self.buffer.add(
            session_id=session_id,
            chunk_id=chunk_id,
            data=pcm,
            is_end=is_end,
        )

        # запускаем обработку сессии
        await self.process_session(session_id)

        elapsed_ms = (time.perf_counter() - started) * 1000

        logger.info(
            "s3_read session_id=%s chunk_id=%s is_end=%s bytes=%s duration_ms=%.2f",
            session_id,
            chunk_id,
            is_end,
            len(audio),
            elapsed_ms,
        )

    async def process_session(self, session_id: str):
        async with self.session_locks[session_id]:

            # streaming: отдаем всё что готово
            while True:
                chunk = await self.buffer.pop_if_ready(
                    session_id,
                    min_ms=160,
                )

                if chunk is None:
                    break

                logger.info(
                    "ASR chunk session=%s bytes=%s",
                    session_id,
                    len(chunk),
                )

                await self.asr.send(
                    session_id,
                    chunk,
                    is_last=False,
                )

            # финализация
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

                # cleanup
                self.buffer.buf.pop(session_id, None)
                self.buffer.locks.pop(session_id, None)

                self.asr.started.discard(session_id)
                self.asr.seq_map.pop(session_id, None)

                self.session_locks.pop(session_id, None)


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