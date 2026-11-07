from __future__ import annotations

from typing import Optional, List, Tuple
from collections import deque

import asyncio
from datetime import datetime
import uuid
from zoneinfo import ZoneInfo

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

# Pre-roll configuration
SAMPLE_RATE = 16000
PRE_ROLL_DURATION_SEC = 3.0
PRE_ROLL_SAMPLES = int(SAMPLE_RATE * PRE_ROLL_DURATION_SEC)  # 48000 samples = 3.0 sec


class PreRollBuffer:
    """
    Кольцевой буфер для pre-roll аудио с абсолютной нумерацией сэмплов.
    
    Каждый элемент: (sample_start: int, audio_bytes: bytes)
    sample_start - абсолютный номер первого сэмпла в чанке
    """
    
    def __init__(self, max_samples: int):
        self.max_samples = max_samples
        self.buffer: deque = deque()
        self.total_samples = 0
    
    def add_chunk(self, sample_start: int, audio_bytes: bytes) -> None:
        """Добавить чанк в буфер с абсолютным номером первого сэмпла."""
        self.buffer.append((sample_start, audio_bytes))
        self.total_samples += len(audio_bytes) // 2  # 2 bytes per sample (int16)
        
        # Удаляем старые данные, если буфер переполнен
        while self.total_samples > self.max_samples:
            old_sample_start, old_audio = self.buffer.popleft()
            self.total_samples -= len(old_audio) // 2
    
    def get_preroll(
        self, 
        current_sample_start: int,
        last_used_sample_end: Optional[int] = None,
        debug: bool = False
    ) -> List[Tuple[int, bytes]]:
        """
        Получить pre-roll данные, исключая уже использованные сэмплы.
        
        Args:
            current_sample_start: Абсолютный номер первого сэмпла текущего чанка (is_begin)
            last_used_sample_end: Последний использованный сэмпл в предыдущей сессии (exclusive)
            debug: Включить детальное логирование
            
        Returns:
            Список (sample_start, audio_bytes) для отправки в новую сессию
        """
        result = []
        
        if debug:
            print(f"[DEBUG get_preroll] buffer has {len(self.buffer)} chunks")
            print(f"[DEBUG get_preroll] current_sample_start={current_sample_start}, last_used_sample_end={last_used_sample_end}")
        
        for sample_start, audio_bytes in self.buffer:
            sample_end = sample_start + len(audio_bytes) // 2
            
            if debug:
                print(f"[DEBUG get_preroll] checking chunk [{sample_start}-{sample_end}]")
            
            # Пропускаем чанки, которые полностью до last_used_sample_end
            if last_used_sample_end is not None and sample_end <= last_used_sample_end:
                if debug:
                    print(f"[DEBUG get_preroll] SKIP: chunk ends before last_used_end")
                continue
            
            # Пропускаем чанки, которые начинаются после или в начале текущего чанка
            # (текущий чанк будет отправлен отдельно после обработки is_begin)
            if sample_start >= current_sample_start:
                if debug:
                    print(f"[DEBUG get_preroll] BREAK: chunk starts at or after current")
                break
            
            # Если чанк частично перекрывается с last_used_sample_end, обрезаем
            if last_used_sample_end is not None and sample_start < last_used_sample_end < sample_end:
                # Вычисляем смещение для обрезки
                skip_samples = last_used_sample_end - sample_start
                skip_bytes = skip_samples * 2
                audio_bytes = audio_bytes[skip_bytes:]
                sample_start = last_used_sample_end
                if debug:
                    print(f"[DEBUG get_preroll] TRIM: chunk trimmed to [{sample_start}-{sample_start+len(audio_bytes)//2}]")
            
            if len(audio_bytes) > 0:
                result.append((sample_start, audio_bytes))
                if debug:
                    print(f"[DEBUG get_preroll] ADD: chunk [{sample_start}-{sample_start+len(audio_bytes)//2}]")
        
        return result
    
    def clear(self) -> None:
        """Очистить буфер."""
        self.buffer.clear()
        self.total_samples = 0


class VADGateway(bridge_pb2_grpc.AudioBridgeServicer):
    def __init__(self, storage_stub):
        self.storage = storage_stub

        # 🔥 один клиент как в working script
        self.vad_client = grpcclient.InferenceServerClient(url=URL)

        self.seq_map = {}
        self.triton_seq_map = {}
        self.recording = {}
        self.active_sessions = {}

        # Global pre-roll buffer (shared across all base_session_id)
        self._preroll_buffer = PreRollBuffer(PRE_ROLL_SAMPLES)
        self._last_used_sample_end: dict[str, int] = {}  # Track last used sample end per base_session_id
        self._global_sample_counter = 0  # Global sample counter (shared across all base_session_id)

    # -------------------------
    # CLEAN INFER (как working script)
    # -------------------------
    def _run_vad(
        self,
        audio_np: np.ndarray,
        sequence_id: int,
        sequence_start: bool,
    ) -> Response:
        threshold = np.array([[0.1]], dtype=np.float16)
        min_silence = np.array([[1500]], dtype=np.int16)
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
            sequence_id=sequence_id,
            sequence_start=sequence_start,
            sequence_end=False,
        )

        output = result.as_numpy("Response").item()
        return Response.FromString(output)

    # -------------------------
    # ENTRYPOINT
    # -------------------------

    async def StreamMic(self, request_iterator, context):
        current_base_session_id = None

        try:
            async for chunk in request_iterator:
                base_session_id = chunk.session_id
                current_base_session_id = base_session_id

                vad_session_id = self.active_sessions.get(base_session_id, base_session_id)
                triton_state = self.triton_seq_map.get(base_session_id)
                if triton_state is None:
                    triton_state = {
                        "sequence_id": uuid.uuid4().int & ((1 << 63) - 1),
                        "sequence": 0,
                    }
                    self.triton_seq_map[base_session_id] = triton_state

                sequence_start = triton_state["sequence"] == 0
                triton_state["sequence"] += 1

                # Use global sample counter (shared across all base_session_id)
                current_sample_start = self._global_sample_counter
                num_samples = len(chunk.audio) // 2  # 2 bytes per sample (int16)
                
                audio_np = np.frombuffer(chunk.audio, dtype=np.int16).reshape(1, -1)

                vad_response = await asyncio.to_thread(
                    self._run_vad,
                    audio_np,
                    triton_state["sequence_id"],
                    sequence_start,
                )

                is_begin = False
                is_end = False
                closed_vad_session_id = None
                closed_vad_seq_id = 0

                for mark in vad_response.va_marks:
                    if mark.mark_type == 1:
                        is_begin = True
                    elif mark.mark_type == 2:
                        is_end = True

                # Add current chunk to global pre-roll buffer BEFORE VAD processing
                # This ensures pre-roll contains current chunk when is_begin is detected
                self._preroll_buffer.add_chunk(current_sample_start, chunk.audio)
                
                # Debug: log buffer state (every 10 chunks to avoid spam)
                if current_sample_start % 24000 == 0:  # Every 10 chunks
                    buffer_info = f"buffer_size={len(self._preroll_buffer.buffer)}"
                    if len(self._preroll_buffer.buffer) > 0:
                        first_ss, _ = self._preroll_buffer.buffer[0]
                        last_ss, _ = self._preroll_buffer.buffer[-1]
                        buffer_info += f" range=[{first_ss}-{last_ss+len(self._preroll_buffer.buffer[-1][1])//2}]"
                        print(f"[BUFFER DEBUG] {buffer_info}")
                    else:
                        print(f"[BUFFER DEBUG] buffer is empty")
                
                if is_begin and base_session_id not in self.active_sessions:
                    vad_session_id = (
                        f"{datetime.now(ZoneInfo('Europe/Moscow')):%Y%m%d-%H%M%S}-"
                        f"{base_session_id}-"
                        f"{uuid.uuid4()}"
                    )

                    self.active_sessions[base_session_id] = vad_session_id
                    self.seq_map[vad_session_id] = 0
                    self.recording[vad_session_id] = True
                    
                    # 🔥 PRE-ROLL LOGIC: Extract pre-roll data for new session
                    last_used_end = self._last_used_sample_end.get(base_session_id)
                    
                    # Debug: show buffer state before get_preroll
                    print(f"[PRE-ROLL] Session: {vad_session_id}")
                    print(f"[PRE-ROLL] buffer_size={len(self._preroll_buffer.buffer)}")
                    if len(self._preroll_buffer.buffer) > 0:
                        chunks_info = ", ".join(f"[{s}-{s+len(a)//2}]" for s, a in list(self._preroll_buffer.buffer)[-5:])
                        print(f"[PRE-ROLL] buffer chunks: {chunks_info}")
                    print(f"[PRE-ROLL] Current sample start: {current_sample_start}")
                    print(f"[PRE-ROLL] Last used sample end (exclusive): {last_used_end}")
                    
                    preroll_chunks = self._preroll_buffer.get_preroll(
                        current_sample_start, last_used_end, debug=True
                    )
                    
                    # Log diagnostic info
                    total_preroll_samples = sum(len(c[1]) // 2 for c in preroll_chunks)
                    excluded_samples = 0
                    if last_used_end is not None and current_sample_start > last_used_end:
                        excluded_samples = current_sample_start - last_used_end
                    
                    print(f"[PRE-ROLL] Pre-roll chunks: {len(preroll_chunks)}")
                    print(f"[PRE-ROLL] Pre-roll samples: {total_preroll_samples}")
                    print(f"[PRE-ROLL] Excluded samples (already used): {excluded_samples}")
                    
                    # Send pre-roll chunks first
                    for preroll_sample_start, preroll_audio in preroll_chunks:
                        self.seq_map[vad_session_id] += 1
                        preroll_chunk = audio_pb2.AudioChunk(
                            session_id=vad_session_id,
                            sequence=self.seq_map[vad_session_id],
                            audio=preroll_audio,
                            sample_rate=chunk.sample_rate,
                            is_begin=True,
                            is_end=False,
                            timestamp_ms=0,
                            encoding="pcm_s16le",
                        )
                        await self.storage.StreamAudio(iter([preroll_chunk]))
                    
                    # Mark pre-roll chunks as sent (update last_used_sample_end)
                    if preroll_chunks:
                        last_preroll_sample_end = preroll_chunks[-1][0] + len(preroll_chunks[-1][1]) // 2
                        self._last_used_sample_end[base_session_id] = last_preroll_sample_end
                        print(f"[PRE-ROLL] Updated last_used_sample_end to: {last_preroll_sample_end}")

                # -------------------------
                # WRITE DECISION AFTER STATE UPDATE
                # -------------------------
                if self.recording.get(vad_session_id, None) is not None:
                    print("vad_session_id", vad_session_id, "is_begin", is_begin, "is_end", is_end)
                    self.seq_map[vad_session_id] += 1

                    enriched_chunk = audio_pb2.AudioChunk(
                        session_id=vad_session_id,
                        sequence=self.seq_map[vad_session_id],
                        audio=chunk.audio,
                        sample_rate=chunk.sample_rate,
                        is_begin=is_begin,
                        is_end=is_end,
                        timestamp_ms=0,
                        encoding="pcm_s16le",
                    )

                    await self.storage.StreamAudio(iter([enriched_chunk]))

                if is_end and vad_session_id:
                    closed_vad_session_id = vad_session_id
                    closed_vad_seq_id = self.seq_map.get(vad_session_id, 0)
                    
                    # Update last_used_sample_end to current chunk end
                    current_sample_end = current_sample_start + num_samples
                    self._last_used_sample_end[base_session_id] = current_sample_end
                    print(f"[SESSION END] Updated last_used_sample_end to: {current_sample_end}")
                    
                    self.recording.pop(vad_session_id, None)
                    self.seq_map.pop(vad_session_id, None)
                    self.active_sessions.pop(base_session_id, None)
                
                # Update global sample counter
                self._global_sample_counter += num_samples
                
                # -------------------------
                # CLIENT EVENT
                # -------------------------
                if vad_session_id not in self.seq_map:
                    vad_session_id = "None"
                event_session_id = closed_vad_session_id if closed_vad_session_id is not None else vad_session_id
                event_seq_id = closed_vad_seq_id if closed_vad_session_id is not None else self.seq_map.get(vad_session_id, 0)
                yield bridge_pb2.VadEvent(
                    session_id=event_session_id,
                    sequence=event_seq_id,
                    is_begin=is_begin,
                    is_end=is_end,
                )
        finally:
            if current_base_session_id is not None:
                self.triton_seq_map.pop(current_base_session_id, None)

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
