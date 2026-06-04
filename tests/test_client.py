import asyncio
import grpc
import time
import random

from generated_old import audio_pb2
from generated_old import audio_pb2_grpc


def make_audio_chunk(size=320):
    # fake PCM int16 audio
    return bytes([random.randint(0, 255) for _ in range(size)])


async def run():
    channel = grpc.aio.insecure_channel("localhost:50051")
    stub = audio_pb2_grpc.AudioIngestionStub(channel)

    session_id = f"test-session-{int(time.time())}"

    async def request_generator():
        print("🚀 START STREAM")

        # 1. BEGIN
        yield audio_pb2.AudioChunk(
            session_id=session_id,
            audio=b"",
            sample_rate=16000,
            is_begin=True,
            is_end=False,
        )

        # 2. AUDIO CHUNKS
        for i in range(20):
            chunk = make_audio_chunk()

            print(f"🎧 sending chunk {i}, size={len(chunk)}")

            yield audio_pb2.AudioChunk(
                session_id=session_id,
                audio=chunk,
                sample_rate=16000,
                is_begin=False,
                is_end=False,
            )

            await asyncio.sleep(0.05)

        # 3. END
        print("🏁 END STREAM")

        yield audio_pb2.AudioChunk(
            session_id=session_id,
            audio=b"",
            sample_rate=16000,
            is_begin=False,
            is_end=True,
        )

    response = await stub.StreamAudio(request_generator())

    print("\n✅ RESPONSE:")
    print("session_id:", response.session_id)
    print("chunks:", response.received_chunks)
    print("bytes:", response.received_bytes)
    print("s3_key:", response.s3_key)


if __name__ == "__main__":
    asyncio.run(run())