import sys
import uuid
import queue
import threading
from queue import Empty
from zoneinfo import ZoneInfo
from datetime import datetime

import grpc
import sounddevice as sd

import bridge_pb2
import bridge_pb2_grpc

import logging

logger = logging.getLogger(__name__)


SAMPLE_RATE = 16000
CHUNK_MS = 150
STORE_ID = 1
WORKER_NAME = "DANIIL_SUETIN"

audio_queue = queue.Queue()
logger = logging.getLogger(__name__)
def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s "
            "%(levelname)s "
            "%(name)s "
            "%(message)s"
        ),
        stream=sys.stdout,
    )
configure_logging()

def audio_callback(indata, frames, time, status):
    if status:
        logger.info("Audio status=%s", status)
    audio_queue.put(indata.copy())


def make_session_id() -> str:
    return f"{STORE_ID}-{WORKER_NAME}"


def mic_stream(session_id: str, stop_event: threading.Event):
    blocksize = int(SAMPLE_RATE * CHUNK_MS / 1000)
    logger.info(
        "Session started. session_id=%s",
        session_id,
    )

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


            logger.debug(
                "Chunk sent. session_id=%s samples=%s",
                session_id,
                len(audio),
            )

            yield bridge_pb2.MicChunk(
                session_id=session_id,
                audio=audio.tobytes(),
                sample_rate=SAMPLE_RATE,
                is_begin=first_chunk,
                is_end=False,
            )

            first_chunk = False

    logger.info(
        "Session stopped. session_id=%s",
        session_id,
    )


def main():
    channel = grpc.insecure_channel("localhost:6000")
    stub = bridge_pb2_grpc.AudioBridgeStub(channel)

    logger.info("Streaming mic to vad")

    while True:
        session_id = make_session_id()
        stop_event = threading.Event()

        try:
            stream = stub.StreamMic(mic_stream(session_id, stop_event))

            for msg in stream:
                logger.debug(
                    "Chunk sent. session_id=%s seq=%s is_begin=%s is_end=%s",
                    session_id,
                    msg.sequence,
                    msg.is_begin,
                    msg.is_end,
                )

                if msg.is_begin:
                    logger.info(
                        "Speech start. session_id=%s",
                        msg.session_id,
                    )

                if msg.is_end:
                    logger.info(
                        "Speech end. session_id=%s",
                        msg.session_id,
                    )

        except grpc.RpcError as e:
            logger.error("Grpc error. session_id=%s error=%s", session_id, e)

        finally:
            stop_event.set()


if __name__ == "__main__":
    main()