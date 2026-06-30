import asyncio
import json
import logging
import os

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from .state import SessionState
from .classifier import (
    update,
    score,
    best_label,
    threshold_hit,
)

logger = logging.getLogger(__name__)


class ClassificationService:

    def __init__(self):
        self.bootstrap = os.getenv(
            "KAFKA_BOOTSTRAP_SERVERS",
            "redpanda:9092",
        )

        self.in_topic = os.getenv(
            "KAFKA_INPUT_TOPIC",
            "asr_transcripts",
        )

        self.out_topic = os.getenv(
            "KAFKA_OUTPUT_TOPIC",
            "classified_events",
        )

        self.group_id = os.getenv(
            "KAFKA_GROUP_ID",
            "classification",
        )

        self.consumer = None
        self.producer = None

        self.sessions: dict[str, SessionState] = {}

    async def start(self):

        logger.info("Init Kafka consumer...")
        print("before consumer")
        self.consumer = AIOKafkaConsumer(
            self.in_topic,
            bootstrap_servers=self.bootstrap,
            group_id=self.group_id,
            auto_offset_reset="latest",
        )
        print("before producer")
        self.producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap,
        )
        print("before consumer start")
        await self.consumer.start()
        print("before producer start")
        await self.producer.start()

        logger.info("🔥 CLASSIFICATION STARTED")

    def get_state(self, session_id: str) -> SessionState:

        if session_id not in self.sessions:
            self.sessions[session_id] = SessionState()

        return self.sessions[session_id]

    async def emit(
        self,
        session_id,
        text,
        label,
        mode,
        buy,
        ret,
        svc,
    ):

        event = {
            "session_id": session_id,
            "text": text,
            "label": label,
            "score": max(buy, ret, svc),
            "mode": mode,
            "counters": {
                "buy": buy,
                "return": ret,
                "service": svc,
            },
        }

        logger.info("EMIT %s", event)

        await self.producer.send_and_wait(
            self.out_topic,
            json.dumps(event).encode(),
        )

    async def handle(self, msg):

        raw = msg.value.decode() if isinstance(msg.value, bytes) else msg.value
        event = json.loads(raw)

        session_id = event["session_id"]
        text = event.get("text", "")
        is_final = event.get("is_final", False)

        state = self.get_state(session_id)

        buy, ret, svc = score(state)
        print("before = ", text, buy, ret, svc)
        #
        # обновляем гистограмму
        #
        update(state, text, is_final)

        #
        # считаем веса
        #
        buy, ret, svc = score(state)
        print("text after update = ", text, buy, ret, svc)
        label, score_value = best_label(
            buy,
            ret,
            svc,
        )

        logger.info(
            "session=%s buy=%s return=%s service=%s label=%s",
            session_id,
            buy,
            ret,
            svc,
            label,
        )

        #
        # threshold
        #
        if (
            threshold_hit(buy, ret, svc)
            and not state.threshold_sent
        ):
            print("emit", session_id, text, label, "threshold", buy, ret, svc,)
            await self.emit(
                session_id,
                text,
                label,
                "threshold",
                buy,
                ret,
                svc,
            )

            state.threshold_sent = True

        #
        # смена сценария
        #
        if (
            state.last_label is None
            or (
                label != state.last_label
                and score_value >= state.last_score
            )
        ):

            await self.emit(
                session_id,
                text,
                label,
                "switch",
                buy,
                ret,
                svc,
            )

            state.last_label = label
            state.last_score = score_value

        #
        # финальное сообщение ASR
        #
        if is_final:

            await self.emit(
                session_id,
                text,
                label,
                "final",
                buy,
                ret,
                svc,
            )

            #
            # следующая волна threshold
            #
            state.threshold_sent = False

    async def run(self):

        await self.start()

        try:

            async for msg in self.consumer:
                await self.handle(msg)

        finally:

            if self.consumer:
                await self.consumer.stop()

            if self.producer:
                await self.producer.stop()


def main():
    service = ClassificationService()
    asyncio.run(service.run())


if __name__ == "__main__":
    main()