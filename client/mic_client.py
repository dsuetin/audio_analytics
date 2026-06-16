import uuid
import queue
from zoneinfo import ZoneInfo
import grpc
import sounddevice as sd

import bridge_pb2
import bridge_pb2_grpc

from datetime import datetime, UTC


SAMPLE_RATE = 16000
# CHUNK_MS = 2000
CHUNK_MS = 150

audio_queue = queue.Queue()


# ----------------------------
# MIC CALLBACK
# ----------------------------
def audio_callback(indata, frames, time, status):
    if status:
        print(status)

    audio_queue.put(indata.copy())


# ----------------------------
# MIC STREAM GENERATOR
# ----------------------------
def mic_stream():
    # session_id = str(uuid.uuid4())
    session_id = (
        f"{datetime.now(ZoneInfo('Europe/Moscow')):%Y%m%d-%H%M%S}-"
        f"{uuid.uuid4()}"
    )
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
        while True:
            audio = audio_queue.get()
            print("📦 sending chunk", len(audio))
            yield bridge_pb2.MicChunk(
                session_id=session_id,
                audio=audio.tobytes(),
                sample_rate=SAMPLE_RATE,
                is_begin=first_chunk,
                is_end=False,
            )

        first_chunk = False


# ----------------------------
# MAIN
# ----------------------------
def main():
    channel = grpc.insecure_channel("localhost:6000")
    stub = bridge_pb2_grpc.AudioBridgeStub(channel) 

    print("🚀 streaming MIC → VAD CLIENT") 

    try:
        response = stub.StreamMic(mic_stream())
        print("\nACK:", response)

    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()