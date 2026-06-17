from aiokafka import AIOKafkaConsumer
import asyncio
import json
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("asr-worker")


KAFKA_BOOTSTRAP = "redpanda:9092"
TOPIC = "audio_events"   # твой kafka topic


async def process_message(msg):
    try:
        event = json.loads(msg.value.decode("utf-8"))

        if event.get("type") != "audio_chunk_saved":
            return

        session_id = event["session_id"]
        payload = event["payload"]

        logger.info(
            "🎧 ASR MOCK RECEIVED chunk "
            "session_id=%s s3_key=%s chunk_id=%s",
            session_id,
            payload.get("s3_key"),
            payload.get("chunk_id"),
        )

        # тут позже будет ASR inference
        # text = await asr.infer(...)

    except Exception as e:
        logger.exception("failed to process kafka message: %s", e)


async def main():
    consumer = AIOKafkaConsumer(
        "audio_events",
        bootstrap_servers="redpanda:9092",
        group_id="asr-worker-debug",
        auto_offset_reset="earliest",
        enable_auto_commit=True,
    )

    await consumer.start()
    print("🚀 CONSUMER STARTED")

    try:
        async for msg in consumer:
            print("🔥 RAW MSG RECEIVED")
            await process_message(msg)

    except Exception as e:
        print("💥 FATAL LOOP ERROR:", e)

    finally:
        await consumer.stop()


if __name__ == "__main__":
    asyncio.run(main())