import asyncio
import json
import logging
import os

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

logger = logging.getLogger(__name__)


class ClassificationService:

    def __init__(self):
        self.bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "redpanda:9092")
        self.in_topic = os.getenv("KAFKA_INPUT_TOPIC", "asr_transcripts")
        self.out_topic = os.getenv("KAFKA_OUTPUT_TOPIC", "classified_events")

        self.consumer = None
        self.producer = None

    async def start(self):
        # 🔥 ВАЖНО: создаём ЗДЕСЬ
        print("INIT consumer service")
        self.consumer = AIOKafkaConsumer(
            self.in_topic,
            bootstrap_servers=self.bootstrap,
            group_id="classification",
            auto_offset_reset="latest",
        )
        print("INIT  producer service")
        self.producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap
        )
        print("START  producer service")
        await self.consumer.start()

        print("START  producer service")
        await self.producer.start()

        logger.info("🔥 CLASSIFICATION STARTED")

    async def handle(self, msg):
        raw = msg.value.decode() if isinstance(msg.value, bytes) else msg.value
        event = json.loads(raw)

        logger.info("EVENT %s", event)

        out = {
            "session_id": event["session_id"],
            "text": event.get("text", ""),
            "label": "buy",
        }

        print("out = ", out)

        await self.producer.send_and_wait(
            self.out_topic,
            json.dumps(out).encode(),
        )

    async def run(self):
        # 🔥 ВАЖНО: сначала start()
        await self.start()

        try:
            async for msg in self.consumer:
                print("msg", msg)
                await self.handle(msg)

        finally:
            await self.consumer.stop()
            await self.producer.stop()


def main():
    service = ClassificationService()
    asyncio.run(service.run())


if __name__ == "__main__":
    main()