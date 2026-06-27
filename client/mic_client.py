import sys
import queue
import threading
from queue import Empty

import grpc
import sounddevice as sd

import bridge_pb2
import bridge_pb2_grpc

import logging
import asyncio
import json

from aiokafka import AIOKafkaConsumer


# ---------------- LOGGING ----------------
logger = logging.getLogger(__name__)

def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

configure_logging()


# ---------------- CONFIG ----------------
SAMPLE_RATE = 16000
CHUNK_MS = 150
STORE_ID = 1
WORKER_NAME = "DANIIL_SUETIN"

audio_queue = queue.Queue()


def audio_callback(indata, frames, time, status):
    if status:
        logger.info("Audio status=%s", status)
    audio_queue.put(indata.copy())


def make_session_id() -> str:
    return f"{STORE_ID}-{WORKER_NAME}"


def mic_stream(session_id: str, stop_event: threading.Event):
    blocksize = int(SAMPLE_RATE * CHUNK_MS / 1000)

    logger.info("Session started session_id=%s", session_id)

    first_chunk = True

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=blocksize,
        callback=audio_callback,
    ):
        while not stop_event.is_set():
            try:
                audio = audio_queue.get(timeout=0.1)
            except Empty:
                continue

            yield bridge_pb2.MicChunk(
                session_id=session_id,
                audio=audio.tobytes(),
                sample_rate=SAMPLE_RATE,
                is_begin=first_chunk,
                is_end=False,
            )

            first_chunk = False

    logger.info("Session stopped session_id=%s", session_id)



def log_event(message: str, session_id: str):
    # если сейчас рисуется "живая" строка ASR,
    # сначала завершаем ее переводом строки
    sys.stdout.write("\n")
    sys.stdout.flush()

    logger.info("%s %s", message, session_id)

# ---------------- KAFKA ----------------

def print_live(text: str):
    sys.stdout.write("\r\033[2K")   # очистить текущую строку
    sys.stdout.write(text)
    sys.stdout.flush()


async def kafka_listener():
    consumer = AIOKafkaConsumer(
        "asr_transcripts",
        bootstrap_servers="localhost:19092",
        group_id="mic-client",
        auto_offset_reset="latest",
    )

    await consumer.start()
    logger.info("Kafka consumer started")

    try:
        async for msg in consumer:
            raw = msg.value
            # aiokafka может вернуть bytes или str
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")

            event = json.loads(raw)
            if event["is_final"]:
                icon = "🏁"   # клетчатый флаг
                print_live(
                   f"{icon} {event['session_id']}: {event['text']}"
                )
                print()
            else:
                icon = "⌨️"   # печатная машинка
                print_live(
                   f"{icon} {event['session_id']}: {event['text']}"
                )

    finally:
        await consumer.stop()

def start_kafka():
    asyncio.run(kafka_listener())
# ---------------- MAIN ----------------
async def main():
    # Kafka runs independently
    threading.Thread(target=start_kafka, daemon=True).start()
    

    channel = grpc.insecure_channel("localhost:6000")
    stub = bridge_pb2_grpc.AudioBridgeStub(channel)

    loop = asyncio.get_running_loop()

    while True:
        session_id = make_session_id()
        stop_event = threading.Event()

        # run gRPC stream in thread (IMPORTANT)
        stream = await loop.run_in_executor(
            None,
            lambda: stub.StreamMic(mic_stream(session_id, stop_event))
        )

        try:
            for msg in stream:

                # ✅ VAD EVENTS
                if msg.is_begin:
                    log_event("🟢 SPEECH START", msg.session_id)

                if msg.is_end:
                    log_event("🔴 SPEECH END  ", msg.session_id)

        except grpc.RpcError as e:
            logger.error("gRPC error: %s", e)

        finally:
            stop_event.set()


if __name__ == "__main__":
    asyncio.run(main())