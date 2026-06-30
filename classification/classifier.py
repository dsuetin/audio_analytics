import asyncio
import json
import logging
import sys

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from .state import SessionState
from .classifier import update, score, best_label, threshold_hit
from .policy import (
    KAFKA_BOOTSTRAP_SERVERS,
    INPUT_TOPIC,
    OUTPUT_TOPIC,
    GROUP_ID,
)

logger = logging.getLogger(__name__)


class ClassificationService:

    def __init__(self):
        self.sessions = {}

        self.consumer = AIOKafkaConsumer(
            INPUT_TOPIC,
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            group_id=GROUP_ID,
            auto_offset_reset="latest",
        )

        self.producer = AIOKafkaProducer(
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        )

    def get_state(self, session_id: str):
        if session_id not in self.sessions:
            self.sessions[session_id] = SessionState()
        return self.sessions[session_id]

    async def emit(self, event: dict):
        await self.producer.send_and_wait(
            OUTPUT_TOPIC,
            json.dumps(event).encode(),
        )

    async def handle(self, msg):

        raw = msg.value.decode() if isinstance(msg.value, bytes) else msg.value
        event = json.loads(raw)

        session_id = event["session_id"]
        text = event.get("text", "")
        is_final = event.get("is_final", True)

        state = self.get_state(session_id)

        # 1. update histogram
        update(state, text)

        # 2. score
        buy, ret, svc = score(state)
        label, score_val = best_label(buy, ret, svc)

        # -------------------
        # THRESHOLD
        # -------------------
        if threshold_hit(buy, ret, svc) and not state.threshold_sent:

            await self.emit({
                "session_id": session_id,
                "text": text,
                "label": label,
                "mode": "threshold",
                "counters": {
                    "buy": buy,
                    "return": ret,
                    "service": svc,
                }
            })

            state.threshold_sent = True

        # -------------------
        # SWITCH
        # -------------------
        if state.last_label is None or (
            label != state.last_label and score_val >= state.last_score
        ):

            await self.emit({
                "session_id": session_id,
                "text": text,
                "label": label,
                "mode": "switch",
                "counters": {
                    "buy": buy,
                    "return": ret,
                    "service": svc,
                }
            })

            state.last_label = label
            state.last_score = score_val

        # -------------------
        # FINAL
        # -------------------
        if is_final:

            await self.emit({
                "session_id": session_id,
                "text": text,
                "label": label,
                "mode": "final",
                "counters": {
                    "buy": buy,
                    "return": ret,
                    "service": svc,
                }
            })

            # reset threshold gate after final
            state.threshold_sent = False

    async def run(self):
        await self.consumer.start()
        await self.producer.start()

        logger.info("🔥 CLASSIFICATION STARTED")

        try:
            async for msg in self.consumer:
                await self.handle(msg)
        finally:
            await self.consumer.stop()
            await self.producer.stop()


def main():
    asyncio.run(ClassificationService().run())


if __name__ == "__main__":
    main()