from __future__ import annotations

import asyncio
import uuid

import grpc
import numpy as np
import tritonclient.grpc as grpcclient

import bridge_pb2
import bridge_pb2_grpc
import audio_pb2
import audio_pb2_grpc

from vad_pb.output_pb2 import Response


class VADGateway(bridge_pb2_grpc.AudioBridgeServicer):
    def __init__(self, storage_stub):
        self.storage = storage_stub

        self.vad_client = grpcclient.InferenceServerClient(
            url="server_triton-speech-segmentation:8001"
        )

        # sequence number для storage
        self.seq_map = {}

        # состояние VAD-сессий
        self.vad_sessions = {}

    # --------------------------------------------------
    # VAD
    # --------------------------------------------------
    def _run_vad(
        self,
        audio_np: np.ndarray,
        sequence_id: int,
        sequence_start: bool,
        sequence_end: bool,
    ) -> Response:

        # audio_np = np.asarray(audio_np, dtype=np.int16)

        # # 🔥 ЖЁСТКАЯ ГАРАНТИЯ ФОРМЫ (как в working script)
        # if audio_np.ndim == 1:
        #     audio_np = np.expand_dims(audio_np, axis=0)

        # if audio_np.shape[0] != 1:
        #     audio_np = audio_np.T  # fallback защита

        # assert audio_np.shape[1] == 2400 or audio_np.shape[0] == 1

        # print("BEFORE INFER", audio_np.shape, flush=True)

        audio_np = np.frombuffer(audio_np, dtype=np.int16)
        audio_np = np.expand_dims(audio_np, axis=0)
        audio_np = np.ascontiguousarray(audio_np)

        print("FINAL SHAPE:", audio_np.shape)

        threshold = np.array([[0.2]], dtype=np.float16)
        min_silence = np.array([[500]], dtype=np.int16)
        mode = np.array([[b"ONLY_SPEECH"]])

        infer_inputs = [
            # grpcclient.InferInput("audio", list(audio_np.shape), "INT16"),
            grpcclient.InferInput("audio", [1, 2400], "INT16"),
            grpcclient.InferInput("threshold", [1, 1], "FP16"),
            grpcclient.InferInput("min_silence_ms", [1, 1], "INT16"),
            grpcclient.InferInput("mode", [1, 1], "BYTES"),
        ]

        infer_inputs[0].set_data_from_numpy(audio_np)
        infer_inputs[1].set_data_from_numpy(threshold)
        infer_inputs[2].set_data_from_numpy(min_silence)
        infer_inputs[3].set_data_from_numpy(mode)

        result = self.vad_client.infer(
            "online_vad",
            infer_inputs,
            outputs=[grpcclient.InferRequestedOutput("Response")],
            sequence_id=sequence_id,
            sequence_start=sequence_start,
            sequence_end=False,   # 🔥 КРИТИЧНО как в working script
        )

        output = result.as_numpy("Response")

        # 🔥 ВАЖНО: как в working code
        response = output_pb2.Response.FromString(output.item())

        return response

    # --------------------------------------------------
    # STORAGE STREAM
    # --------------------------------------------------
    async def storage_stream(self, request_iterator):
        print("🟡 STORAGE STREAM STARTED")

        async for chunk in request_iterator:
            session_id = chunk.session_id

            # ----------------------------------
            # storage sequence
            # ----------------------------------
            if session_id not in self.seq_map:
                self.seq_map[session_id] = 0

            self.seq_map[session_id] += 1
            seq = self.seq_map[session_id]

            # ----------------------------------
            # vad session state
            # ----------------------------------
            if session_id not in self.vad_sessions:
                self.vad_sessions[session_id] = {
                    "corr_id": int(uuid.uuid4().int % 1_000_000_000),
                    "chunk_idx": 0,
                }

            state = self.vad_sessions[session_id]

            corr_id = state["corr_id"]
            chunk_idx = state["chunk_idx"]

            # PCM -> numpy
            audio_np = np.frombuffer(
                chunk.audio,
                dtype=np.int16,
            ).reshape(1, -1)

            print(
                f"🎙️ session={session_id} "
                f"corr_id={corr_id} "
                f"chunk_idx={chunk_idx} "
                f"shape={audio_np.shape}"
            )

            # ----------------------------------
            # VAD
            # ----------------------------------
            print(
                f"shape={audio_np.shape} "
                f"min={audio_np.min()} "
                f"max={audio_np.max()} "
                f"mean_abs={np.abs(audio_np).mean():.2f}"
            )
            vad_response = await asyncio.to_thread(
                self._run_vad,
                audio_np,
                corr_id,
                chunk_idx == 0,   # start only once
                False,            # end later
            )

            state["chunk_idx"] += 1
            print("vad_response", vad_response)
            print(
                f"🎙️ VAD response "
                f"session={session_id} "
                f"marks={len(vad_response.va_marks)}"
            )

            is_begin = False
            is_end = False

            for mark in vad_response.va_marks:
                print(
                    f"mark={mark.mark_type} "
                    f"offset={mark.offset_ms}"
                )

                if mark.mark_type == 1:
                    is_begin = True

                elif mark.mark_type == 2:
                    is_end = True

            print(
                f"📦 session={session_id} "
                f"seq={seq} "
                f"begin={is_begin} "
                f"end={is_end}"
            )

            yield audio_pb2.AudioChunk(
                session_id=session_id,
                sequence=seq,
                audio=chunk.audio,
                sample_rate=chunk.sample_rate,
                is_begin=is_begin,
                is_end=is_end,
                timestamp_ms=0,
                encoding="pcm_s16le",
            )

            # ----------------------------------
            # cleanup
            # ----------------------------------
            if is_end:
                print(
                    f"🏁 END session={session_id}"
                )
                self.seq_map.pop(session_id, None)
                self.vad_sessions.pop(session_id, None)

                break

    # --------------------------------------------------
    # gRPC ENTRYPOINT
    # --------------------------------------------------
    async def StreamMic(self, request_iterator, context):
        print("🔥 STREAMMIC STARTED")

        async def gen():
            async for chunk in self.storage_stream(
                request_iterator
            ):
                print("2️⃣ FROM VAD")
                yield chunk

        response = await self.storage.StreamAudio(
            gen()
        )

        return response


async def serve():
    channel = grpc.aio.insecure_channel(
        "worker:50051"
    )

    storage_stub = (
        audio_pb2_grpc.AudioIngestionStub(channel)
    )

    server = grpc.aio.server()

    bridge_pb2_grpc.add_AudioBridgeServicer_to_server(
        VADGateway(storage_stub),
        server,
    )

    server.add_insecure_port("[::]:6000")

    await server.start()

    print(
        "🔥 VAD CLIENT STARTED",
        flush=True,
    )

    await server.wait_for_termination()


if __name__ == "__main__":
    asyncio.run(serve())