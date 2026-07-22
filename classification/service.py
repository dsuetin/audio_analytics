import asyncio
import json
import logging
import os

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
import asyncpg

from .state import StateManager
from .classifier import (
    update,
    score,
    best_label,
    threshold_hit,
)
from .metadata import parse_session_metadata

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

        self.db = None


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

        print("before db connect")
        self.db = await asyncpg.connect(
            host=os.getenv("POSTGRES_HOST", "postgres"),
            port=int(os.getenv("POSTGRES_PORT", 5432)),
            user=os.getenv("POSTGRES_USER", "speech"),
            password=os.getenv("POSTGRES_PASSWORD", "speech"),
            database=os.getenv("POSTGRES_DB", "speech_db"),
        )
        logger.info("🔥 CLASSIFICATION STARTED")


    async def emit(
        self,
        session_id,
        chunk_id,
        is_final,
        text,
        label,
        mode,
        buy,
        ret,
        svc,
        matched_words,
        store_id,
    ):
        print("emit", session_id, chunk_id, is_final, text, label, mode, buy, ret, svc)
        event = {
            "session_id": session_id,
            "store_id": store_id,
            "chunk_id": chunk_id,
            "is_final": is_final,           
            "text": text,
            "label": label,
            "score": max(buy, ret, svc),
            "mode": mode,
            "counters": {
                "buy": buy,
                "return": ret,
                "service": svc,
            },
            "matched_words": matched_words,
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
        metadata = parse_session_metadata(event)
        store_id = metadata["store_id"]
        text = event.get("text", "")
        chunk_id = event.get("chunk_id")
        is_final = event.get("is_final", False)

        store_state = self.state.store(store_id)
        session_state = store_state.session(session_id)
        client_state = store_state.client(store_state.current_client_id)
        new_session = store_state.dialog.process(text, is_final)

        if new_session:

            print("\n========== NEW CLIENT ==========\n")
            client_id = f"client_{len(store_state.clients)+1}"
            store_state.current_client_id = client_id
            print("new client_id", client_id)
            client_state = store_state.client(client_id)
            await self.save_client_id(session_id, client_id)
            store_state.threshold_sent = False
            store_state.last_label = None
            store_state.last_score = 0
            if self.producer is not None and not isinstance(self.producer, type(None)):
                await self.producer.send_and_wait(
                    "new_client_session",
                    json.dumps(
                        {
                            "type": "new_session",
                            "store_id": store_id,
                            "client_id": client_id,
                        }
                    ).encode()
                )
            else:
                logger.error("Producer is not initialized or is None")
        
        if is_final:
            store_state.active_sessions.discard(session_id)
        else:
            store_state.active_sessions.add(session_id)

        if not client_state:
            return None
        #
        # обновляем гистограмму
        #
        update(client_state, session_state, text, is_final)
        working = client_state.confirmed.copy()

        for sid in store_state.active_sessions:
            working += store_state.session(sid).partial

        print("\nWORKING:")
        buy, ret, svc, matched_words = score(working)
        print("session buy, ret, svc", buy, ret, svc)

        label, score_value = best_label(
            buy,
            ret,
            svc,
        )

        print("best label", label, score_value)

        logger.info(
            "session=%s buy=%s return=%s service=%s label=%s",
            session_id,
            buy,
            ret,
            svc,
            label,
        )

        # threshold
        if (
            threshold_hit(buy, ret, svc)
            and not store_state.threshold_sent
        ):
            print("emit", session_id, text, label, "threshold", buy, ret, svc,)
            await self.emit(
                session_id,
                chunk_id,
                is_final,
                text,
                label,
                "threshold",
                buy,
                ret,
                svc,
                matched_words,
                store_id,
            )

            store_state.threshold_sent = True
            store_state.last_label = label
            store_state.last_score = score_value
            await self.save_dialog_type(session_id, label)

        
        # смена сценария
        
        if (store_state.threshold_sent
            and label != store_state.last_label
            and score_value > store_state.last_score):

            await self.emit(
                session_id,
                chunk_id,
                is_final,
                text,
                label,
                "switch",
                buy,
                ret,
                svc,
                matched_words,
                store_id,
            )

            store_state.last_label = label
            store_state.last_score = score_value
            await self.save_dialog_type(session_id, label)

        


    async def save_dialog_type(self, session_id: str, dialog_type: str):
        await self.db.execute(
            """
            UPDATE transcripts
            SET dialog_type = $2
            WHERE session_id = $1
            """,
            session_id,
            dialog_type,
        )

        
    async def save_client_id(self, session_id: str, client_id: str):
        await self.db.execute(
            """
            UPDATE transcripts
            SET client_id = $2
            WHERE session_id = $1
            """,
            session_id,
            client_id,
        )    
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

            if self.db:
                await self.db.close()

    
def main():
    service = ClassificationService()
    asyncio.run(service.run())


if __name__ == "__main__":
    main()
