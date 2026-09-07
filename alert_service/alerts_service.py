import os
import json
import logging
import asyncio
import asyncpg

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from alert_service.notifications import create_notification_sender
from alert_service.metadata import parse_session_metadata
from alert_service.config import (
    TRIGGER_PHRASES,
    BUY_PHRASES,
    SALESPERSON_PHRASES,
)
from alert_service.text_matcher import find_phrase
logger = logging.getLogger(__name__)




class AlertService:

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
            "KAFKA_ALERT_TOPIC",
            "alerts",
        )

        self.purchase_topic = os.getenv(
            "KAFKA_PURCHASE_TOPIC",
            "purchases",
        )

        self.salesperson_topic = os.getenv(
            "KAFKA_SALESPERSON_TOPIC",
            "salesperson_changes",
        )

        self.consumer = None
        self.producer = None
        self.db = None

        # чтобы одно событие не стреляло 100 раз
        self.fired_sessions = set()
        self.purchase_sessions = set()
        self.pending_salesperson = set()

        # Канал доставки уведомлений (telegram | max).
        # Бизнес-логика не знает деталей API мессенджера — только NotificationSender.
        self.notification_sender = create_notification_sender()
        logger.info(
            "📣 notification channel: %s",
            self.notification_sender.name,
        )


    async def start(self):

        self.consumer = AIOKafkaConsumer(
            self.in_topic,
            bootstrap_servers=self.bootstrap,
            group_id="alerts-service",
            auto_offset_reset="latest",
        )

        self.producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap,
        )


        await self.consumer.start()
        await self.producer.start()


        self.db = await asyncpg.connect(
            host=os.getenv("POSTGRES_HOST", "postgres"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            user=os.getenv("POSTGRES_USER", "speech"),
            password=os.getenv("POSTGRES_PASSWORD", "speech"),
            database=os.getenv("POSTGRES_DB", "speech_db"),
        )
        logger.info("🚨 ALERT SERVICE STARTED")


    async def send_notification(self, text):

        try:
            await asyncio.to_thread(
                self.notification_sender.send,
                text,
            )

        except Exception:
            logger.exception(
                "notification send error"
            )


    async def save_alarm(self, session_id):

        await self.db.execute(
            """
            UPDATE transcripts
            SET is_alarm_triggered=TRUE
            WHERE session_id=$1
            """,
            session_id,
        )


    async def save_purchase(self, session_id):

        await self.db.execute(
            """
            UPDATE transcripts
            SET is_sale=TRUE
            WHERE session_id=$1
            """,
            session_id,
        )


    async def save_salesperson_change(
        self,
        session_id,
        name,
    ):

        await self.db.execute(
            """
            UPDATE transcripts
            SET seller_id=$2
            WHERE session_id=$1
            """,
            session_id,
            name,
        )


    async def emit(
        self,
        topic,
        payload,
    ):

        await self.producer.send_and_wait(
            topic,
            json.dumps(payload).encode(),
        )


    async def handle(self, msg):

        event = json.loads(
            msg.value.decode()
        )


        session_id = event["session_id"]

        text = event.get(
            "text",
            "",
        )


        if not text:
            return


        metadata = parse_session_metadata(event)

        store_id = metadata["store_id"]


        #
        # objection
        #
        phrase = find_phrase(
            text,
            TRIGGER_PHRASES,
        )


        if (
            phrase
            and session_id not in self.fired_sessions
        ):

            self.fired_sessions.add(session_id)


            payload = {
                "session_id": session_id,
                "store_id": store_id,
                "text": text,
                "phrase": phrase,
                "type": "objection_trigger",
            }


            await asyncio.gather(
                self.emit(
                    self.out_topic,
                    payload,
                ),
                self.save_alarm(session_id),
                self.send_notification(
                    f"🚨 Alert\n\n"
                    f"Store: {store_id}\n"
                    f"Session: {session_id}\n\n"
                    f"{text}"
                ),
            )


            logger.warning(
                "🚨 ALERT %s",
                payload,
            )

            return



        #
        # purchase
        #
        phrase = find_phrase(
            text,
            BUY_PHRASES,
        )


        if (
            phrase
            and session_id not in self.purchase_sessions
        ):

            self.purchase_sessions.add(session_id)


            payload = {
                "session_id": session_id,
                "store_id": store_id,
                "text": text,
                "phrase": phrase,
                "type": "purchase",
            }


            await asyncio.gather(
                self.emit(
                    self.purchase_topic,
                    payload,
                ),
                self.save_purchase(session_id),
            )


            logger.warning(
                "💰 PURCHASE %s",
                payload,
            )

            return



        #
        # salesperson
        #
        phrase = find_phrase(
            text,
            SALESPERSON_PHRASES,
        )
        if phrase:
            self.pending_salesperson.add(session_id)

        #
        # ждем финальную фразу
        #
        if (
            event.get("is_final")
            and session_id in self.pending_salesperson
        ):

            self.pending_salesperson.remove(session_id)

            words = text.lower().split()

            for i in range(len(words) - 3):
                if (
                    words[i] == "имя"
                    and words[i + 1] in ("продавца", "консультанта")
                ):
                    name = "_".join(words[i + 2:i + 4])

                    payload = {
                        "session_id": session_id,
                        "store_id": store_id,
                        "text": text,
                        "type": "salesperson_change",
                        "new_salesperson": name,
                    }

                    await asyncio.gather(
                        self.emit(
                            self.salesperson_topic,
                            payload,
                        ),
                        self.save_salesperson_change(
                            session_id,
                            name,
                        ),
                    )

                    logger.info(
                        "👤 SALESPERSON %s",
                        payload,
                    )

                    break


    async def run(self):

        await self.start()

        try:

            async for msg in self.consumer:
                await self.handle(msg)

        finally:

            await self.consumer.stop()
            await self.producer.stop()

            if self.db:
                await self.db.close()



def main():

    logging.basicConfig(
        level=logging.INFO,
    )

    asyncio.run(
        AlertService().run()
    )


if __name__ == "__main__":
    main()
