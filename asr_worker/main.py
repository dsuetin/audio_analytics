import asyncio
import logging
import sys
import time

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

    async def handle_event(self, event: dict):
        if event.get("type") != "audio_chunk_saved":
            return

        session_id = event["session_id"]
        chunk_id = event["payload"]["chunk_id"]
        s3_key = event["payload"]["s3_key"]

        started = time.perf_counter()

        audio = self.s3.get_object(s3_key)

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
            chunk=audio,
        )


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