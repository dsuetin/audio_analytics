import asyncio
import json
import logging
import os

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from .state import StateManager
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

        # self.sessions: dict[str, SessionState] = {}
        self.state = StateManager()

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

        session_state = self.state.session(session_id)
        client_state = self.state.client("SUETIN_DANIIL")

        # buy, ret, svc = score(state)

        print("\nCONFIRMED:")
        buy, ret, svc = score(client_state.confirmed)
        print("client buy, ret, svc", buy, ret, svc)
        print("\nWORKING:")
        buy, ret, svc = score(session_state.working)
        print("session buy, ret, svc", buy, ret, svc)
        #
        # обновляем гистограмму
        #
        update(client_state, session_state, text, is_final)

        #
        # считаем веса
        #
        # buy, ret, svc = score(state)
        # print("text after update = ", text, buy, ret, svc)
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
            and not self.state.threshold_sent
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

            self.state.threshold_sent = True

        #
        # смена сценария
        #
        # if (
        #     self.state.last_label is None
        #     or (
        #         label != self.state.last_label
        #         and score_value >= self.state.last_score
        #     )
        # ):

        #     await self.emit(
        #         session_id,
        #         text,
        #         label,
        #         "switch",
        #         buy,
        #         ret,
        #         svc,
        #     )

        #     self.state.last_label = label
        #     self.state.last_score = score_value

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
            self.state.threshold_sent = False

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