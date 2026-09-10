from __future__ import annotations

import asyncio
import logging
from aiokafka import AIOKafkaProducer

from storage_worker.events import Event

logger = logging.getLogger(__name__)


class KafkaEventProducer:
    def __init__(self, bootstrap_servers: str, topic: str):
        self.bootstrap_servers = bootstrap_servers
        self.topic = topic


    async def start(self) -> None:
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap_servers,
            request_timeout_ms=30000,
            retry_backoff_ms=500,
            metadata_max_age_ms=5000,
        )
        await self._producer.start()
        logger.info("Kafka producer started")

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def send(self, event: Event) -> None:
        if self._producer is None:
            raise RuntimeError("Kafka producer is not started")
        try:
            await self._producer.send_and_wait(
                self.topic,
                value=event.to_json_bytes(),
                key=event.session_id.encode(),
            )
        except Exception:
            logger.exception("Failed to publish event")
