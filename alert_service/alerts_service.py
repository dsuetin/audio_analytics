import os
import json
import logging
import asyncio
import asyncpg
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
import numpy as np
from sentence_transformers import SentenceTransformer
from alert_service.telegram_bot import TelegramBot
from alert_service.metadata import parse_session_metadata


TRIGGER_THRESHOLD = 0.90
BUY_THRESHOLD = 0.95
SALESPERSON_THRESHOLD = 0.75

logger = logging.getLogger(__name__)

TRIGGER_PHRASES = [
    "я еще похожу посмотрю что в других отделах предлагают",
    "мне нужно посоветоваться с мужем",
    "мне нужно посоветоваться с женой",
    "мне нужно посоветоваться с мастером",
    "слишком дорого я рассчитывал на сумму в два раза меньше",
    "запишите мне название и модель я дома почитаю отзывы",
    "я вам сам перезвоню когда приму решение",
    "мне кажется мой старый еще можно подзарядить",
    "я подожду до зарплаты следующего месяца",
    "поискать что нибудь другое у вас мало выбора",
    "в интернете я видел такой же но дешевле",
    "не уверен что мне подойдет",
    "гарантия всего год маловато",
    "я просто прицениваюсь",
    "друг говорил что быстро умирают",
    "поищу посвежее",
    "боюсь не влезет",
    "подожду акций",
    "перезвоню позже",
    "не готов сейчас принимать решение",
    "директор магазина",
    "директор по рекламациям",
    "директор по сервису",
    "персональное предложение",
    "индивидуальный подход",
    "помогите",
]

BUY_PHRASES = [
    "оформляем","беру","оплачиваю","давайте оформим","покупаю",
    "забираю","оформляйте","меня устраивает","берем",
    "оплачу картой","спасибо за покупку",
]

SALESPERSON_PHRASES = [
    "имя продавца",
    "имя консультанта",
]

class AlertService:
    def __init__(self):
        self.bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "redpanda:9092")
        self.in_topic = os.getenv("KAFKA_INPUT_TOPIC", "asr_transcripts")
        self.out_topic = os.getenv("KAFKA_ALERT_TOPIC", "alerts")
        self.purchase_topic = os.getenv("KAFKA_PURCHASE_TOPIC", "purchases")
        self.salesperson_topic = os.getenv("KAFKA_SALESPERSON_TOPIC", "salesperson_changes")
        self.consumer = None
        self.producer = None
        self.db = None
        print("Loading sentence transformer model...")
        self.model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2",device="cpu")
        print("Model loaded.")
        self.trigger_embeddings = np.array(self.model.encode(TRIGGER_PHRASES, normalize_embeddings=True))
        self.buy_embeddings = np.array(self.model.encode(BUY_PHRASES, normalize_embeddings=True))
        self.salesperson_embeddings = np.array(self.model.encode(SALESPERSON_PHRASES, normalize_embeddings=True))
        self.fired_sessions = set()
        self.purchase_sessions = set()
        self.salesperson_sessions = set()
        self.telegram = TelegramBot(os.getenv("TELEGRAM_TOKEN"))
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "114987350")

    async def start(self):
        self.consumer = AIOKafkaConsumer(self.in_topic,bootstrap_servers=self.bootstrap,group_id="alerts-service",auto_offset_reset="latest")
        self.producer = AIOKafkaProducer(bootstrap_servers=self.bootstrap)
        await self.consumer.start()
        await self.producer.start()
        self.db=await asyncpg.connect(
            host=os.getenv("POSTGRES_HOST", "postgres"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            user=os.getenv("POSTGRES_USER", "speech"),
            password=os.getenv("POSTGRES_PASSWORD", "speech"),
            database=os.getenv("POSTGRES_DB", "speech_db"),
        )
        logger.info("🚨 ALERT SERVICE STARTED")


    async def send_telegram(self, text: str):

        if not self.telegram_chat_id:
            logger.warning("Telegram chat id not configured")
            return

        try:
            result = await asyncio.to_thread(
                self.telegram.send_message,
                self.telegram_chat_id,
                text
            )

            logger.info(
                f"Telegram sent: {result}"
            )

        except Exception:
            logger.exception(
                "Telegram send failed"
            )
    def cosine_max_similarity(self,text,embeddings):
        if not text: return 0.0
        emb = self.model.encode(text, normalize_embeddings=True)
        return float(np.max(np.dot(embeddings, emb)))


    def detect_scores(self, text: str, is_final: bool):
        if not is_final:
            return None

        text = text.lower().strip()

        if len(text) < 2:
            return None

        emb = self.model.encode(text, normalize_embeddings=True)

        objection_score = float(np.max(self.trigger_embeddings @ emb))
        purchase_score = float(np.max(self.buy_embeddings @ emb))
        salesperson_score = float(np.max(self.salesperson_embeddings @ emb))

        return {
            "objection": objection_score,
            "purchase": purchase_score,
            "salesperson": salesperson_score,
        }

    async def save_alarm(self,session_id):
        await self.db.execute("UPDATE transcripts SET is_alarm_triggered=TRUE WHERE session_id=$1",session_id)

    async def save_purchase(self,session_id):
        await self.db.execute("UPDATE transcripts SET is_sale=TRUE WHERE session_id=$1",session_id)

    async def save_salesperson_change(self, session_id: str, name: str):
        try:
            await self.db.execute(
                """
                UPDATE transcripts
                SET seller_id = $2
                WHERE session_id = $1
                """,
                session_id, name
            )

            logger.info(f"SALESPERSON UPDATED: {session_id}")

        except Exception:
            logger.exception("Failed to update salesperson")


    async def handle(self, msg):
        event = json.loads(
            msg.value.decode() if isinstance(msg.value, bytes) else msg.value
        )

        session_id = event.get("session_id")
        metadata = parse_session_metadata(event)
        store_id = metadata["store_id"]
        text = event.get("text", "")
        print(f"Received event: {event}")
        scores = self.detect_scores(
            text=text,
            is_final=event.get("is_final", False),
        )

        if scores is None:
            return

        #
        # objection
        #
        if (
            scores["objection"] >= TRIGGER_THRESHOLD
            and session_id not in self.fired_sessions
        ):
            self.fired_sessions.add(session_id)

            payload = {
                "session_id": session_id,
                "store_id": store_id,
                "text": text,
                "score": scores["objection"],
                "type": "objection_trigger",
            }

            await asyncio.gather(
                self.emit(self.out_topic, payload),
                self.save_alarm(session_id),
                self.send_telegram(
                    f"🚨 Alert\n\n"
                    f"Store: {store_id}\n"
                    f"Session: {session_id}\n"
                    f"Phrase:\n{text}\n\n"
                    f"Score: {scores['objection']:.3f}"
                ),
            )

            logger.warning(f"🚨 TRIGGER FIRED: {payload}")
            return

        #
        # purchase
        #
        if (
            scores["purchase"] >= BUY_THRESHOLD
            and session_id not in self.purchase_sessions
        ):
            self.purchase_sessions.add(session_id)

            payload = {
                "session_id": session_id,
                "store_id": store_id,
                "text": text,
                "score": scores["purchase"],
                "type": "purchase",
            }

            await asyncio.gather(
                self.emit(self.purchase_topic, payload),
                self.save_purchase(session_id),
            )

            logger.warning(f"💰 PURCHASE: {payload}")
            return

        #
        # salesperson
        #
        if (
            scores["salesperson"] >= SALESPERSON_THRESHOLD
            and session_id not in self.salesperson_sessions
        ):
            self.salesperson_sessions.add(session_id)

            name = "_".join(text.split()[-2:])

            payload = {
                "session_id": session_id,
                "store_id": store_id,
                "text": text,
                "score": scores["salesperson"],
                "type": "salesperson_change",
                "new_salesperson": name,
            }

            await asyncio.gather(
                self.emit(self.salesperson_topic, payload),
                self.save_salesperson_change(session_id, name),
            )

            logger.warning(
                f"👤 SALESPERSON: {payload}, name={name}"
            )
            

    async def emit(self, topic: str, payload: dict):
        await self.producer.send_and_wait(
            topic,
            json.dumps(payload).encode(),
        )

    async def run(self):
        await self.start()
        try:
            async for msg in self.consumer:
                await self.handle(msg)
        finally:
            await self.consumer.stop()
            await self.producer.stop()
            self.fired_sessions.clear()
            self.purchase_sessions.clear()
            self.salesperson_sessions.clear()
            if self.db:
                await self.db.close()

def main():
    logging.basicConfig(level=logging.INFO)
    asyncio.run(AlertService().run())

if __name__=="__main__":
    main()
