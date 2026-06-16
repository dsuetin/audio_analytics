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


SAMPLE_RATE = 16000
CHUNK_MS = 150
STORE_ID = 1
WORKER_NAME = "DANIIL_SUETIN"

audio_queue = queue.Queue()


def audio_callback(indata, frames, time, status):
    if status:
        print(status)
    audio_queue.put(indata.copy())


def make_session_id() -> str:
    # return f"{datetime.now(ZoneInfo('Europe/Moscow')):%Y%m%d-%H%M%S}-{uuid.uuid4()}"
    return f"{STORE_ID}-{WORKER_NAME}"


def mic_stream(session_id: str, stop_event: threading.Event):
    blocksize = int(SAMPLE_RATE * CHUNK_MS / 1000)
    print(f"🎤 session started: {session_id}")

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

            print("📦 sending chunk", len(audio))

            yield bridge_pb2.MicChunk(
                session_id=session_id,
                audio=audio.tobytes(),
                sample_rate=SAMPLE_RATE,
                is_begin=first_chunk,
                is_end=False,
            )

            first_chunk = False

    print(f"🛑 session stopped: {session_id}")


def main():
    channel = grpc.insecure_channel("localhost:6000")
    stub = bridge_pb2_grpc.AudioBridgeStub(channel)

    print("🚀 streaming MIC → VAD CLIENT")

    while True:
        session_id = make_session_id()
        stop_event = threading.Event()

        try:
            stream = stub.StreamMic(mic_stream(session_id, stop_event))

            for msg in stream:
                print(
                    f"📦 seq={msg.sequence} "
                    f"begin={msg.is_begin} "
                    f"end={msg.is_end}"
                )

                if msg.is_begin:
                    print("🟢 SPEECH START")

                if msg.is_end:
                    print("🔴 SPEECH END")
                    stop_event.set()
                    break

        except grpc.RpcError as e:
            print("RPC error:", e)

        finally:
            stop_event.set()


if __name__ == "__main__":
    main()