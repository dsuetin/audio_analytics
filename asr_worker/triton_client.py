import asyncio

import numpy as np
import tritonclient.grpc.aio as grpcclient
from tritonclient.utils import np_to_triton_dtype
from asr_worker.proto.output_pb2 import SpeechRecognitionHypothesis


ONLINE_MODEL = "emformer_conformer_online_tdt_punct_microphone_v1"
FINAL_MODEL = "emformer_conformer_online_finalize_tdt_punct_microphone_v1"


class TritonASRClient:
    def __init__(self, url="triton-asr:8001"):
        self.client = grpcclient.InferenceServerClient(url=url)
        self.started = set()
        self.seq_map = {}

    def _seq_id(self, session_id: str) -> int:
        if session_id not in self.seq_map:
            self.seq_map[session_id] = abs(hash(session_id)) & 0x7FFFFFFF
        return self.seq_map[session_id]

    def _input(self, pcm: bytes):
        audio = np.frombuffer(pcm, dtype=np.int16)[None, :]
        print(audio.shape)
        print(audio.dtype)
        inp = grpcclient.InferInput(
            "audio",
            audio.shape,
            np_to_triton_dtype(audio.dtype),
        )
        inp.set_data_from_numpy(audio)
        return inp

    async def send(
        self,
        session_id: str,
        pcm: bytes,
        is_last: bool = False,
    ):
        seq_id = self._seq_id(session_id)

        seq_start = session_id not in self.started

        if seq_start:
            self.started.add(session_id)

        model_name = FINAL_MODEL if is_last else ONLINE_MODEL

        result = await self.client.infer(
            model_name=model_name,
            inputs=[self._input(pcm)],
            outputs=[
                grpcclient.InferRequestedOutput(
                    "SpeechRecognitionHypothesis"
                )
            ],
            sequence_id=seq_id,
            sequence_start=seq_start,
            sequence_end=is_last,
        )

        print("after client send")
        raw = result.as_numpy("SpeechRecognitionHypothesis")
        print("raw result received", raw)
        if raw is None:
            return ""
        # scalar ndarray
        if raw.shape == ():
            payload = raw.item()
        else:
            payload = raw[0]

        print("raw result received", payload)

        if not payload:
            print("empty hypothesis received")
            return ""

        hyp = SpeechRecognitionHypothesis()
        print("RAW OUTPUT:", raw)
        print("TYPE:", type(raw))
        print("SHAPE:", getattr(raw, "shape", None))
        print("before parse")
        hyp.ParseFromString(payload)
        print("after parse")

        return hyp.normalized_transcript or hyp.transcript

    async def finalize(self, session_id: str):
        seq_id = self._seq_id(session_id)

        await self.client.infer(
            model_name=FINAL_MODEL,
            inputs=[self._input(b"")],
            outputs=[
                grpcclient.InferRequestedOutput("SpeechRecognitionHypothesis")
            ],
            sequence_id=seq_id,
            sequence_start=False,
            sequence_end=True,
        )

        self.started.discard(session_id)