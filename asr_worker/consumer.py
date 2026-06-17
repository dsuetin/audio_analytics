from aiokafka import AIOKafkaConsumer
import json
import logging

logger = logging.getLogger("asr-worker")


class KafkaConsumerWrapper:
    def __init__(self, bootstrap, topic):
        self.bootstrap = bootstrap
        self.topic = topic
        self._consumer: AIOKafkaConsumer | None = None  # 👈 ВАЖНО

    async def start(self):
        self._consumer = AIOKafkaConsumer(
            self.topic,
            bootstrap_servers=self.bootstrap,
            group_id="asr-worker-v1",
            auto_offset_reset="earliest",
        )

        await self._consumer.start()
        logger.info("🚀 CONSUMER STARTED")

    async def run(self, handler):
        if self._consumer is None:
            raise RuntimeError("Consumer not started")

        async for msg in self._consumer:   # 👈 ТОЛЬКО ЭТО
            event = json.loads(msg.value.decode("utf-8"))
            await handler(event)

    async def stop(self):
        if self._consumer:
            await self._consumer.stop()