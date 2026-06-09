import asyncio
import grpc
import time
import random

from generated_old import audio_pb2
from generated_old import audio_pb2_grpc


def make_audio_chunk(size=320):
    return bytes(random.getrandbits(8) for _ in range(size))


async def run():
    rpc_started = time.perf_counter()

    channel = grpc.aio.insecure_channel("localhost:50051")
    stub = audio_pb2_grpc.AudioIngestionStub(channel)

    session_id = f"test-session-{int(time.time())}"

    async def request_generator():
        stream_started = time.perf_counter()

        print(
            f"[{time.perf_counter()-rpc_started:8.3f}] "
            f"🚀 START STREAM"
        )

        begin_chunk = audio_pb2.AudioChunk(
            session_id=session_id,
            audio=b"",
            sample_rate=16000,
            is_begin=True,
            is_end=False,
        )

        print(
            f"[{time.perf_counter()-rpc_started:8.3f}] "
            f"➡️ BEGIN"
        )

        yield begin_chunk

        for i in range(20):
            chunk = make_audio_chunk()

            now = time.perf_counter()

            print(
                f"[{now-rpc_started:8.3f}] "
                f"➡️ CHUNK {i+1:02d} "
                f"bytes={len(chunk)}"
            )

            yield audio_pb2.AudioChunk(
                session_id=session_id,
                audio=chunk,
                sample_rate=16000,
                is_begin=False,
                is_end=False,
            )

            await asyncio.sleep(0.05)

        print(
            f"[{time.perf_counter()-rpc_started:8.3f}] "
            f"🏁 END STREAM"
        )

        yield audio_pb2.AudioChunk(
            session_id=session_id,
            audio=b"",
            sample_rate=16000,
            is_begin=False,
            is_end=True,
        )

        print(
            f"[{time.perf_counter()-rpc_started:8.3f}] "
            f"📤 GENERATOR FINISHED "
            f"(duration={time.perf_counter()-stream_started:.3f}s)"
        )

    print(
        f"[{time.perf_counter()-rpc_started:8.3f}] "
        f"📞 CALL StreamAudio()"
    )

    call_started = time.perf_counter()

    response = await stub.StreamAudio(
        request_generator()
    )

    call_elapsed = time.perf_counter() - call_started

    print(
        f"[{time.perf_counter()-rpc_started:8.3f}] "
        f"✅ RPC FINISHED "
        f"(rpc_time={call_elapsed:.3f}s)"
    )

    print()
    print("========== RESPONSE ==========")
    print("session_id:", response.session_id)
    print("chunks:", response.received_chunks)
    print("bytes:", response.received_bytes)
    print("s3_key:", response.s3_key)
    print("==============================")


if __name__ == "__main__":
    asyncio.run(run())