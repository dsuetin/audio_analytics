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

import signal

# SERVER_IP = "10.201.0.9"
# SERVER_IP = "192.168.0.10"
SERVER_IP = "localhost"

KAFKA_BOOTSTRAP = f"{SERVER_IP}:19092"
GRPC_ADDR = f"{SERVER_IP}:6000"

CLASS_ICONS = {
    "buy": "🛒",             # покупка
    "service": "🔧",         # сервис
    "complaint": "⚠️",       # рекламация / жалоба
    "corporate": "🏢",       # корпоративные продажи
    "working_hours": "🕒",   # режим работы
    "vacancy": "💼",         # вакансии
    "help": "❓",            # справка / помощь
    "lost": "🧭",            # потерянные вещи
    "other": "💬",           # прочее
}


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
STORE_ID = "main_store"
WORKER_NAME = "иванов_иван"

worker_lock = threading.Lock()
current_stop_event_lock = threading.Lock()
current_stop_event = None  # type: threading.Event | None
shutdown_event = threading.Event()
audio_queue = queue.Queue()

def handle_shutdown(signum, frame):
    logger.info("Received signal %s", signum)
    shutdown_event.set()
    stop_current_session()

def audio_callback(indata, frames, time, status):
    if status:
        logger.info("Audio status=%s", status)
    audio_queue.put(indata.copy())


def make_session_id() -> str:
    with worker_lock:
        worker = WORKER_NAME
    return f"{STORE_ID}-{worker}"


def event_store_id(event: dict) -> str:
    if event.get("store_id") is not None:
        return str(event["store_id"])

    session_id = event.get("session_id", "")
    parts = session_id.split("-")

    if len(parts) >= 8:
        return parts[2]

    return parts[0] if parts else ""


def event_belongs_to_current_store(event: dict) -> bool:
    return event_store_id(event) == str(STORE_ID)


def set_current_stop_event(ev: threading.Event | None):
    global current_stop_event
    with current_stop_event_lock:
        current_stop_event = ev


def stop_current_session():
    with current_stop_event_lock:
        ev = current_stop_event
    if ev is not None:
        ev.set()


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
    sys.stdout.write("\n")
    sys.stdout.flush()
    logger.info("%s %s", message, session_id)


# ---------------- KAFKA ----------------

def print_live(text: str):
    sys.stdout.write("\r\033[2K")
    sys.stdout.write(text)
    sys.stdout.flush()


client_sessions = []


async def kafka_listener():
    consumer = AIOKafkaConsumer(
        "asr_transcripts",
        "classified_events",
        "new_client_session",
        "alerts",
        "purchases",
        "salesperson_changes",
        bootstrap_servers=KAFKA_BOOTSTRAP,
        group_id="mic-client",
        auto_offset_reset="latest",
    )

    await consumer.start()
    logger.info("Kafka consumer started")

    try:
        while not shutdown_event.is_set():
            batches = await consumer.getmany(timeout_ms=50)

            for _, messages in batches.items():
                for msg in messages:
                    raw = msg.value
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")

                    event = json.loads(raw)

                    if not event_belongs_to_current_store(event):
                        continue

                    # -------- Classification --------
                    if msg.topic == "classified_events":
                        if client_sessions:
                            client_sessions[-1] = event["label"]

                        matched = event.get("matched_words", {})
                        parts = []
                        for category, words in matched.items():
                            if words:
                                parts.append(
                                    f"{category}: "
                                    + ", ".join(
                                        f"{w}({c})"
                                        for w, c in sorted(
                                            words.items(),
                                            key=lambda x: x[1],
                                            reverse=True,
                                        )
                                    )
                                )
                        print(
                            "%s | %s",
                            event["label"],
                            " | ".join(parts),
                        )
                        continue

                    # -------- New session --------
                    if msg.topic == "new_client_session":
                        print()
                        client_sessions.append(None)
                        print(
                            "👤 Client changed -> ",
                            len(client_sessions),
                        )
                        continue

                    # -------- Alerts --------
                    if msg.topic in (
                        "alerts",
                        "purchases",
                        "salesperson_changes",
                    ):
                        print()
                        if msg.topic == "alerts":
                            icon = "🚨"
                        elif msg.topic == "purchases":
                            icon = "💰"
                        else:
                            with worker_lock:
                                global WORKER_NAME
                                WORKER_NAME = event.get(
                                    "new_salesperson",
                                    WORKER_NAME,
                                )
                            icon = "👤"

                            # остановить текущую микросессию,
                            # чтобы main() создал новую
                            stop_current_session()

                        print(f"{icon} store={event.get('store_id')} {event.get('session_id','')} {event.get('text', '')}")
                        continue

                    # -------- ASR --------
                    if msg.topic == "asr_transcripts":
                        label = client_sessions[-1] if client_sessions else None
                        class_icon = CLASS_ICONS.get(label, "❔") if label else ""

                        prefix = f"{class_icon} " if class_icon else ""

                        if event["is_final"]:
                            print_live(
                                f"{prefix}🏁 {event['text']}"
                            )
                            print()
                        else:
                            print_live(
                                f"{prefix}🖨️ {event['text']}"
                            )

    finally:
        logger.info("Stopping Kafka consumer...")
        await consumer.stop()
        logger.info("Kafka consumer stopped")


def start_kafka():
    asyncio.run(kafka_listener())


# ---------------- MAIN ----------------
async def main():
    threading.Thread(target=start_kafka, daemon=True).start()

    channel = grpc.insecure_channel(GRPC_ADDR)
    stub = bridge_pb2_grpc.AudioBridgeStub(channel)

    loop = asyncio.get_running_loop()

    while not shutdown_event.is_set():
        session_id = make_session_id()
        stop_event = threading.Event()
        set_current_stop_event(stop_event)

        try:
            stream = await loop.run_in_executor(
                None,
                lambda: stub.StreamMic(mic_stream(session_id, stop_event))
            )

            for msg in stream:
                if msg.is_begin:
                    print("🟢 SPEECH START", msg.session_id)

                if msg.is_end:
                    print()
                    print("🔴 SPEECH END  ", msg.session_id)

        except grpc.RpcError as e:
            logger.error("gRPC error: %s", e)

        finally:
            stop_event.set()
            set_current_stop_event(None)


if __name__ == "__main__":

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    asyncio.run(main())