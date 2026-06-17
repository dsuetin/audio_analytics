import asyncio
import logging

from asr_worker.consumer import KafkaConsumerWrapper
from asr_worker.s3_client import S3Client
from asr_worker.triton_client import TritonASRClient

logging.basicConfig(level=logging.INFO)


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

    async def handle_event(self, event: dict):
        if event.get("type") != "audio_chunk_saved":
            return

        session_id = event["session_id"]
        s3_key = event["payload"]["s3_key"]

        audio = self.s3.get_object(s3_key)

        if session_id not in self.sessions:
            await self.asr.start_session(session_id)
            self.sessions.add(session_id)

        await self.asr.send(session_id, audio)


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