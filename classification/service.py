import asyncio
import json
import logging
import socket

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from .config import (
    KAFKA_BOOTSTRAP,
    INPUT_TOPIC,
    OUTPUT_TOPIC,
    GROUP_ID,
)
print("🔥 CLASSIFICATION BOOTSTRAP START", flush=True)
logger = logging.getLogger(__name__)


# =========================
# SAFE KAFKA WAIT (REAL FIX)
# =========================

async def wait_for_kafka(host: str, port: int, timeout_sec: int = 120):
    logger.info("⏳ Waiting for Kafka %s:%s ...", host, port)

    for i in range(timeout_sec):
        try:
            sock = socket.create_connection((host, port), timeout=2)
            sock.close()
            logger.info("✅ Kafka TCP reachable")
            return
        except OSError:
            logger.info("Kafka not ready... %s/%s", i + 1, timeout_sec)
            await asyncio.sleep(1)

    raise RuntimeError("❌ Kafka not reachable")


def parse_bootstrap(bootstrap: str):
    host, port = bootstrap.split(":")
    return host, int(port)


# =========================
# SIMPLE CLASSIFIER
# =========================

def classify(text: str):
    text = text.lower()

    if "куп" in text:
        return "buy"
    if "возврат" in text or "брак" in text:
        return "return"
    if "ремонт" in text:
        return "service"

    return "service"


# =========================
# MAIN LOOP
# =========================

async def run():

    host, port = parse_bootstrap(KAFKA_BOOTSTRAP)
    print("host", host, "port", port)

    # 🔥 IMPORTANT: block startup until kafka is reachable
    await wait_for_kafka(host, port)

    consumer = AIOKafkaConsumer(
        INPUT_TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP,
        group_id=GROUP_ID,
        auto_offset_reset="latest",
        enable_auto_commit=True,
    )

    producer = AIOKafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP
    )
    print("before consumer")
    await consumer.start()
    print("before producer")
    await producer.start()
    print("after produser")

    logger.info("🚀 classification service started")

    try:
        async for msg in consumer:
            print("msg11111", msg)

            raw = msg.value.decode() if isinstance(msg.value, bytes) else msg.value
            event = json.loads(raw)

            session_id = event["session_id"]
            text = event.get("text", "")

            label = classify(text)

            out = {
                "session_id": session_id,
                "text": text,
                "label": label,
                "is_final": event.get("is_final", True),
            }

            await producer.send_and_wait(
                OUTPUT_TOPIC,
                json.dumps(out).encode(),
            )

            logger.info("📤 %s → %s", session_id, label)

    finally:
        logger.info("🧹 shutting down...")
        await consumer.stop()
        await producer.stop()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()