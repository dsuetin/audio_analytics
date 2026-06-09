import grpc
import asyncio

import bridge_pb2
import bridge_pb2_grpc
import audio_pb2
import audio_pb2_grpc


class VADGateway(bridge_pb2_grpc.AudioBridgeServicer):
    def __init__(self, storage_stub):
        self.storage = storage_stub

    async def StreamMic(self, request_iterator, context):
        print("🔥 STREAMMIC STARTED")

        session_id = None
        chunk_seq = 0
        forwarded_bytes = 0

        async for chunk in request_iterator:
            session_id = chunk.session_id
            chunk_seq += 1
            forwarded_bytes += len(chunk.audio)

            print(
                f"📦 GOT CHUNK "
                f"session={session_id} "
                f"seq={chunk_seq} "
                f"bytes={len(chunk.audio)}"
            )

            storage_chunk = audio_pb2.AudioChunk(
                session_id=session_id,
                sequence=chunk_seq,
                audio=chunk.audio,
                sample_rate=chunk.sample_rate,
                is_begin=(chunk_seq == 1),
                is_end=False,
                timestamp_ms=0,
                encoding="pcm_s16le",
            )

            async def single_chunk():
                yield storage_chunk

            print(
                f"➡️ FORWARD "
                f"session={session_id} "
                f"seq={chunk_seq}"
            )

            await self.storage.StreamAudio(single_chunk())

            if chunk.is_end:
                break

        return bridge_pb2.BridgeAck(
            session_id=session_id or "",
            forwarded_chunks=chunk_seq,
            forwarded_bytes=forwarded_bytes,
        )


async def serve():
    channel = grpc.aio.insecure_channel("worker:50051")
    storage_stub = audio_pb2_grpc.AudioIngestionStub(channel)

    server = grpc.aio.server()

    bridge_pb2_grpc.add_AudioBridgeServicer_to_server(
        VADGateway(storage_stub),
        server,
    )

    server.add_insecure_port("[::]:6000")

    await server.start()

    print("🔥 VAD CLIENT STARTED", flush=True)

    await server.wait_for_termination()


if __name__ == "__main__":
    asyncio.run(serve())