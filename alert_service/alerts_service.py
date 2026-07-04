import os
import json
import logging
import asyncio

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
import numpy as np
from sentence_transformers import SentenceTransformer

TRIGGER_THRESHOLD = 0.82

logger = logging.getLogger(__name__)


TRIGGER_PHRASES = [
    "я еще похожу посмотрю что в других отделах предлагают",
    "мне нужно посоветоваться с мужем женой мастером",
    "слишком дорого я рассчитывал на сумму в два раза меньше",
    "запишите мне название и модель я дома почитаю отзывы",
    "я вам сам перезвоню когда приму решение",
    "мне кажется мой старый еще можно подзарядить",
    "я подожду до зарплаты следующего месяца",
    "поискать что-нибудь другое у вас мало выбора",
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


class AlertService:

    def __init__(self):
        self.bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "redpanda:9092")

        self.in_topic = os.getenv("KAFKA_INPUT_TOPIC", "asr_transcripts")
        self.out_topic = os.getenv("KAFKA_ALERT_TOPIC", "alerts")

        self.consumer = None
        self.producer = None
        print("Loading sentence transformer model...")
        self.model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2",device="cpu")
        print("Model loaded.")
        self.trigger_embeddings = self.trigger_embeddings = np.array(self.model.encode(TRIGGER_PHRASES, normalize_embeddings=True))
        self.fired_sessions = set()
        

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

        logger.info("🚨 ALERT SERVICE STARTED")

    def cosine_max_similarity(self, text: str) -> float:
        if not text:
            return 0.0

        emb = self.model.encode(text, normalize_embeddings=True)

        sims = np.dot(self.trigger_embeddings, emb)

        return float(np.max(sims))




    def is_trigger(self,text: str, is_final: bool) -> tuple[bool, float]:
        if not is_final:
            return False, 0.0

        text = text.lower().strip()

        if len(text) < 2:
            return False, 0.0

        score = self.cosine_max_similarity(text)

        return score >= TRIGGER_THRESHOLD, score

    async def send_to_max(self, session_id: str, text: str, label: str):
        # сюда потом реальный MAX API
        logger.warning(f"📩 MAX MESSAGE: {session_id} | {label} | {text}")

    async def save_db(self, session_id: str, text: str, label: str):
        # TODO postgres insert
        logger.info(f"DB SAVE: {session_id} | {label}")

    async def emit_kafka(self, payload: dict):
        await self.producer.send_and_wait(
            self.out_topic,
            json.dumps(payload).encode(),
        )

    async def handle(self, msg):
        raw = msg.value.decode() if isinstance(msg.value, bytes) else msg.value
        event = json.loads(raw)

        session_id = event.get("session_id")
        text = event.get("text", "")
        is_final = event.get("is_final", False)

        triggered, score = self.is_trigger(text, is_final)

        print(f"session_id={session_id} text={text} is_final={is_final} triggered={triggered} score={score}")
        if session_id in self.fired_sessions:
            return
        if triggered:
            self.fired_sessions.add(session_id)
            payload = {
                "session_id": session_id,
                "text": text,
                "score": score,
                "type": "objection_trigger",
            }

            await self.producer.send_and_wait(
                "alerts",
                json.dumps(payload).encode(),
            )

            await self.save_db(session_id, text, "objection_trigger")


            # await self.send_to_max(session_id, text, trigger)

            logger.warning(f"🚨 TRIGGER FIRED: {payload}")

    async def run(self):
        await self.start()

        try:
            async for msg in self.consumer:
                await self.handle(msg)
        finally:
            await self.consumer.stop()
            await self.producer.stop()
            self.fired_sessions.clear()


def main():
    logging.basicConfig(level=logging.INFO)
    service = AlertService()
    asyncio.run(service.run())


if __name__ == "__main__":
    main()