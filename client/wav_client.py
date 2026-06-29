import asyncio
import json
import logging
import sys
import threading
import time
import uuid
from pathlib import Path

import grpc
import numpy as np
import resampy
import soundfile as sf
from aiokafka import AIOKafkaConsumer

import bridge_pb2
import bridge_pb2_grpc


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
CHUNK_SIZE = int(SAMPLE_RATE * CHUNK_MS / 1000)

WAV_DIR = Path("samples")

KAFKA_BOOTSTRAP = "localhost:19092"
KAFKA_TOPIC = "asr_transcripts"

GRPC_ADDR = "localhost:6000"


# ---------------- SYNC ----------------

last_session_id = None
last_session_finished = threading.Event()


# ---------------- WAV ----------------


def load_wav(path: Path):
    audio, sr = sf.read(path, always_2d=False)

    if audio.ndim == 2:
        audio = audio.mean(axis=1)

    audio = audio.astype(np.float32)

    if sr != SAMPLE_RATE:
        audio = resampy.resample(audio, sr, SAMPLE_RATE)

    audio = np.clip(audio, -1.0, 1.0)

    return (audio * 32767).astype(np.int16)


def make_session_id(filename: str):
    return f"{filename}-{uuid.uuid4()}"


def wav_stream(session_id: str, pcm: np.ndarray):
    logger.info("Session started session_id=%s", session_id)

    first_chunk = True

    for i in range(0, len(pcm), CHUNK_SIZE):

        chunk = pcm[i:i + CHUNK_SIZE]

        if len(chunk) < CHUNK_SIZE:
            chunk = np.pad(
                chunk,
                (0, CHUNK_SIZE - len(chunk)),
                mode="constant",
            )

        yield bridge_pb2.MicChunk(
            session_id=session_id,
            audio=chunk.tobytes(),
            sample_rate=SAMPLE_RATE,
            is_begin=first_chunk,
            is_end=False,
        )

        first_chunk = False

        # эмулируем микрофон
        time.sleep(CHUNK_MS / 1000)

    logger.info("Finished sending %s", session_id)


# ---------------- TERMINAL ----------------


def print_live(text: str):
    sys.stdout.write("\r\033[2K")
    sys.stdout.write(text)
    sys.stdout.flush()


def log_event(message: str, session_id: str):
    sys.stdout.write("\n")
    sys.stdout.flush()
    logger.info("%s %s", message, session_id)


# ---------------- KAFKA ----------------


async def kafka_listener():
    global last_session_id

    consumer = AIOKafkaConsumer(
        KAFKA_TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP,
        group_id="wav-client",
        auto_offset_reset="latest",
    )

    await consumer.start()
    logger.info("Kafka consumer started")

    try:
        async for msg in consumer:

            raw = msg.value

            if isinstance(raw, bytes):
                raw = raw.decode()

            event = json.loads(raw)

            if event["is_final"]:
                print_live(
                    f"🏁 {event['session_id']}: {event['text']}"
                )
                print()

                last_session_finished.set()

            else:
                print_live(
                    f"⌨️ {event['session_id']}: {event['text']}"
                )

    finally:
        await consumer.stop()


def start_kafka():
    asyncio.run(kafka_listener())


# ---------------- MAIN ----------------


def process_file(path: Path):
    global last_session_id

    pcm = load_wav(path)

    session_id = make_session_id(path.name)

    last_session_id = session_id
    last_session_finished.clear()

    print()
    print("=" * 70)
    print(path.name)
    print("=" * 70)

    channel = grpc.insecure_channel(GRPC_ADDR)
    stub = bridge_pb2_grpc.AudioBridgeStub(channel)

    stream = stub.StreamMic(
        wav_stream(session_id, pcm)
    )

    try:
        for msg in stream:

            if msg.is_begin:
                log_event("🟢 SPEECH START", msg.session_id)

            if msg.is_end:
                log_event("🔴 SPEECH END", msg.session_id)

    except grpc.RpcError as e:
        logger.error("gRPC error: %s", e)

    logger.info("Waiting final ASR...")
    last_session_finished.wait()
    logger.info("Final ASR received")


def main():

    threading.Thread(
        target=start_kafka,
        daemon=True,
    ).start()

    time.sleep(2)

    wav_files = sorted(WAV_DIR.glob("*.wav"))

    if not wav_files:
        print("No wav files found")
        return

    for wav in wav_files:
        process_file(wav)


if __name__ == "__main__":
    main()