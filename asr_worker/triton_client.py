import asyncio
from collections import defaultdict

import numpy as np
import tritonclient.grpc.aio as grpcclient
from tritonclient.utils import np_to_triton_dtype

from asr_worker.proto.output_pb2 import SpeechRecognitionHypothesis


ONLINE_MODEL = "emformer_conformer_online_tdt_punct_microphone_v1"
FINAL_MODEL = "emformer_conformer_online_finalize_tdt_punct_microphone_v1"


class TritonASRClient:
    def __init__(self, asr_events, url="triton-asr:8001"):
        self.client = grpcclient.InferenceServerClient(url=url)

        self.streams = {}
        self.queues = defaultdict(asyncio.Queue)
        # self.pending = defaultdict(asyncio.Queue)
        self.tasks = {}

        self.latest_text = defaultdict(str)
        self.started = set()
        self.seq_map = {}
        self.asr_events = asr_events
        self.chunk_ids = defaultdict(int)

    # ----------------------------
    # utils
    # ----------------------------
    def _seq_id(self, session_id: str) -> int:
        if session_id not in self.seq_map:
            self.seq_map[session_id] = abs(hash(session_id)) & 0x7FFFFFFF
        return self.seq_map[session_id]

    def _make_input(self, pcm: bytes):
        audio = np.frombuffer(pcm, dtype=np.int16)[None, :]
        inp = grpcclient.InferInput(
            "audio",
            audio.shape,
            np_to_triton_dtype(audio.dtype),
        )
        inp.set_data_from_numpy(audio)
        return inp

    # ----------------------------
    # stream lifecycle
    # ----------------------------
    async def _inputs_iterator(self, session_id: str):
        seq_id = self._seq_id(session_id)
        seq_start = True
        queue = self.queues[session_id]

        while True:
            item = await queue.get()
            pcm, is_last = item
            # print(f"[{session_id}] Got chunk from queue, is_last={is_last}, bytes={len(pcm)}")
            yield {
                "model_name": ONLINE_MODEL if not is_last else FINAL_MODEL,
                "inputs": [self._make_input(pcm)],
                "outputs": [
                    grpcclient.InferRequestedOutput("SpeechRecognitionHypothesis")
                ],
                "sequence_id": seq_id,
                "sequence_start": seq_start,
                "sequence_end": is_last,
                "parameters": {
                    "interim_results": True,
                    # "pure_online": True,
                },
            }

            seq_start = False

    async def _get_stream(self, session_id: str):
        if session_id in self.streams:
            return self.streams[session_id]

        inputs_iter = self._inputs_iterator(session_id)

        stream = self.client.stream_infer(inputs_iterator=inputs_iter)
        self.streams[session_id] = stream

        self.tasks[session_id] = asyncio.create_task(
            self._consume(session_id, stream)
        )

        return stream

    # ----------------------------
    # response consumer
    # ----------------------------
    async def _consume(self, session_id: str, stream):
        # print("CONSUME STARTED", session_id)
        try:
            chunk_id = 0
            async for response, error in stream:
                # print("GOT RESPONSE")
                if error:
                    print("stream error:", error)
                    continue
                # print("response22222", response)
                # print(response._result)

                params = response._result.parameters

                if "sequence_end" in params:
                    is_final = params["sequence_end"].bool_param
                else:
                    is_final = False
                chunk_id += 1
                raw = response.as_numpy("SpeechRecognitionHypothesis")
                # print(f"[{session_id}] RAW RESPONSE:", raw)
                if raw is None:
                    # print(f"[{session_id}] EMPTY RAW")
                    continue

                payload = raw.item() if raw.shape == () else raw[0]
                # print(f"[{session_id}] PAYLOAD:", payload)
                if not payload:
                    # print(f"[{session_id}] EMPTY PAYLOAD")
                    continue

                hyp = SpeechRecognitionHypothesis()
                hyp.ParseFromString(payload)

                text = hyp.normalized_transcript or hyp.transcript
                self.latest_text[session_id] = text

                # print(f"[{session_id}] ASR:", text)
                # print(f"[{session_id}] ASR chunk_id={chunk_id}, is_final={is_final}, text={text}")
                await self.asr_events.put(
                    {
                        "session_id": session_id,
                        "chunk_id": chunk_id,
                        "text": text,
                        "is_final": is_final,
                    }
                )
        except Exception:
            import traceback
            traceback.print_exc()




    # ----------------------------
    # public API
    # ----------------------------
    async def send(self, session_id: str, pcm: bytes, is_last: bool = False):
        await self._get_stream(session_id)
        self.chunk_ids[session_id] += 1
        await self.queues[session_id].put((pcm, is_last))


    async def finalize(self, session_id: str):
        await self.queues[session_id].put(None)

        if session_id in self.tasks:
            await self.tasks[session_id]

        self.started.discard(session_id)

        self.streams.pop(session_id, None)
        self.queues.pop(session_id, None)
        self.tasks.pop(session_id, None)
        self.chunk_ids.pop(session_id, None)
