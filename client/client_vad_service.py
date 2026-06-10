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

        stats = {
            "session_id": None,
            "chunk_seq": 0,
            "forwarded_bytes": 0,
        }

        async def storage_stream():
            async for chunk in request_iterator:

                stats["session_id"] = chunk.session_id
                stats["chunk_seq"] += 1
                stats["forwarded_bytes"] += len(chunk.audio)

                chunk_seq = stats["chunk_seq"]

                print(
                    f"📦 GOT CHUNK "
                    f"session={chunk.session_id} "
                    f"seq={chunk_seq} "
                    f"bytes={len(chunk.audio)}"
                )

                print(
                    f"➡️ FORWARD "
                    f"session={chunk.session_id} "
                    f"seq={chunk_seq}"
                )

                yield audio_pb2.AudioChunk(
                    session_id=chunk.session_id,
                    sequence=chunk_seq,
                    audio=chunk.audio,
                    sample_rate=chunk.sample_rate,
                    is_begin=(chunk_seq == 1),
                    is_end=chunk.is_end,
                    timestamp_ms=0,
                    encoding="pcm_s16le",
                )

        storage_response = await self.storage.StreamAudio(
            storage_stream()
        )

        print(
            f"✅ STORAGE ACK "
            f"session={storage_response.session_id} "
            f"chunks={storage_response.received_chunks}"
        )

        return bridge_pb2.BridgeAck(
            session_id=stats["session_id"] or "",
            forwarded_chunks=stats["chunk_seq"],
            forwarded_bytes=stats["forwarded_bytes"],
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