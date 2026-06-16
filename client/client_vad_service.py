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


MODEL = "online_vad"
URL = "server_triton-speech-segmentation:8001"


class VADGateway(bridge_pb2_grpc.AudioBridgeServicer):
    def __init__(self, storage_stub):
        self.storage = storage_stub

        # 🔥 один клиент как в working script
        self.vad_client = grpcclient.InferenceServerClient(url=URL)

        self.seq_map = {}

    # -------------------------
    # CLEAN INFER (как working script)
    # -------------------------
    def _run_vad(self, audio_np: np.ndarray):
        threshold = np.array([[0.2]], dtype=np.float16)
        min_silence = np.array([[500]], dtype=np.int16)
        mode = np.array([[b"ONLY_SPEECH"]])

        # 🔥 ЖЁСТКО фиксируем форму
        audio_np = np.asarray(audio_np, dtype=np.int16)
        audio_np = audio_np.reshape(1, -1)

        infer_inputs = [
            grpcclient.InferInput("audio", [1, audio_np.shape[1]], "INT16"),
            grpcclient.InferInput("threshold", [1, 1], "FP16"),
            grpcclient.InferInput("min_silence_ms", [1, 1], "INT16"),
            grpcclient.InferInput("mode", [1, 1], "BYTES"),
        ]

        infer_inputs[0].set_data_from_numpy(audio_np)
        infer_inputs[1].set_data_from_numpy(threshold)
        infer_inputs[2].set_data_from_numpy(min_silence)
        infer_inputs[3].set_data_from_numpy(mode)

        outputs = [grpcclient.InferRequestedOutput("Response")]

        result = self.vad_client.infer(
            MODEL,
            infer_inputs,
            outputs=outputs,
            sequence_id=1,          # 🔥 фиксируем (как stateless)
            sequence_start=True,
            sequence_end=False,
        )

        output = result.as_numpy("Response").item()
        return Response.FromString(output)

    # -------------------------
    # STREAM
    # -------------------------
    async def storage_stream(self, request_iterator):
        async for chunk in request_iterator:

            session_id = chunk.session_id

            self.seq_map.setdefault(session_id, 0)
            self.seq_map[session_id] += 1
            seq = self.seq_map[session_id]

            audio_np = np.frombuffer(chunk.audio, dtype=np.int16).reshape(1, -1)

            vad_response = await asyncio.to_thread(
                self._run_vad,
                audio_np,
            )

            is_begin = False
            is_end = False

            for mark in vad_response.va_marks:
                if mark.mark_type == 1:
                    is_begin = True
                elif mark.mark_type == 2:
                    is_end = True
                mark_to_label = {
                    0: "None",
                    1: "Begin",
                    2: "End",
                }
                label = mark_to_label[mark.mark_type]
                print(f"{mark.offset_ms} ms -> {label}")

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

            # if is_end:
            #     self.seq_map.pop(session_id, None)
            #     break

    # -------------------------
    # ENTRYPOINT
    # -------------------------

    async def StreamMic(self, request_iterator, context):

        async def gen():
            async for chunk in self.storage_stream(request_iterator):
                yield chunk   # VadEvent НЕ тут

        # 🔥 ВАЖНО: storage stream отдельно
        await self.storage.StreamAudio(gen())

        # клиенту возвращаем поток событий
        async for chunk in self.storage_stream(request_iterator):
            yield bridge_pb2.VadEvent(
                session_id=chunk.session_id,
                sequence=chunk.sequence,
                is_begin=chunk.is_begin,
                is_end=chunk.is_end,
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