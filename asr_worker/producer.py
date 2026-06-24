from aiokafka import AIOKafkaProducer
import json


class KafkaProducerWrapper:
    def __init__(self, bootstrap):
        self.bootstrap = bootstrap
        self._producer = None

    async def start(self):
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap,
            acks="all",
            linger_ms=10,
        )
        await self._producer.start()
        print("🔥 PRODUCER STARTED")

    async def send(self, topic: str, value: dict):
        await self._producer.send_and_wait(
            topic,
            json.dumps(value).encode("utf-8"),
        )

    async def stop(self):
        if self._producer:
            await self._producer.stop()