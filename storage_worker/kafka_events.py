from __future__ import annotations

import asyncio
from aiokafka import AIOKafkaProducer

from storage_worker.events import Event


class KafkaEventProducer:
    def __init__(self, bootstrap_servers: str, topic: str):
        self.bootstrap_servers = bootstrap_servers
        self.topic = topic
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap_servers,
            request_timeout_ms=30000,
            retry_backoff_ms=500,
            metadata_max_age_ms=5000,
        )

    async def start(self) -> None:
        print("🔥 Kafka producer starting...")
        await self._producer.start()
        print("✅ Kafka producer started")

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def send(self, event: Event) -> None:
        if self._producer is None:
            raise RuntimeError("Kafka producer is not started")
        await self._producer.send_and_wait(self.topic, event.to_json_bytes())
