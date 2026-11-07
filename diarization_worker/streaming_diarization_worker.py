"""Streaming Diarization Worker — потоковая диаризация с накоплением спикеров по магазинам.

Сервис выполняет:
1. Подписку на Kafka topic audio_events
2. Поддерживает streaming session для каждого магазина (store_id)
3. Накопление спикеров за весь день по магазину
4. Вывод результата в структурированных логах
"""

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set
from queue import Queue
from zoneinfo import ZoneInfo

import asyncpg
import boto3
import numpy as np
from aiokafka import AIOKafkaConsumer
from botocore.config import Config
from tritonclient.grpc import InferenceServerClient, InferInput, InferRequestedOutput
from tritonclient.utils import np_to_triton_dtype

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000  # Hz
BYTES_PER_SAMPLE = 2  # INT16

# Минимальная длительность накопленного аудио для отправки в модель диаризации (мс)
# Короткие фрагменты накапливаются в буфере до достижения этого порога
MIN_AUDIO_DURATION_FOR_DIARIZATION_MS = 3000

# Максимальное время ожидания накопления аудио в буфере (сек)
# После этого времени буфер отправляется в модель даже если не достиг порога
BUFFER_TIMEOUT_SECONDS = 10

# Mapping for VAD mark types
MARK_TO_LABEL = {0: "None", 1: "Begin", 2: "End"}

# Moscow timezone (UTC+3)
MOSCOW_TZ = ZoneInfo('Europe/Moscow')


def hhmmss_to_unix_ms(date_str: str, time_str: str, tz: ZoneInfo = MOSCOW_TZ) -> int:
    """
    Convert date (YYYYmmdd) and time (HHMMSS) from session_id to Unix timestamp (ms).
    
    Args:
        date_str: Date in format YYYYmmdd (e.g., "20261020")
        time_str: Time in format HHMMSS (e.g., "100000")
        tz: Timezone (default: Europe/Moscow = UTC+3)
    
    Returns:
        Unix timestamp in milliseconds
    """
    year = int(date_str[0:4])
    month = int(date_str[4:6])
    day = int(date_str[6:8])
    hour = int(time_str[0:2])
    minute = int(time_str[2:4])
    second = int(time_str[4:6])
    
    dt = datetime(year, month, day, hour, minute, second, tzinfo=tz)
    return int(dt.timestamp() * 1000)


def parse_session_id(session_id: str) -> Dict[str, str]:
    """
    Parse session_id to extract date, time, store_id, seller_id.
    
    Format: YYYYmmdd-HHMMSS-STORE_ID-SELLER_ID-UUID
    
    Returns:
        Dictionary with date, time, store_id, seller_id, uuid
    """
    parts = session_id.split('-')
    if len(parts) >= 5:
        return {
            'date': parts[0],
            'time': parts[1],
            'store_id': parts[2],
            'seller_id': parts[3],
            'uuid': parts[4]
        }
    raise ValueError(f"Invalid session_id format: {session_id}")


@dataclass
class ChunkInfo:
    """Информация о чанке аудио."""
    chunk_id: int
    s3_key: str
    size_bytes: int
    timestamp_ms: int  # Unix timestamp (ms) - время начала чанка
    duration_ms: int   # длительность чанка в ms


@dataclass
class VADSession:
    """VAD сессия с временными метками."""
    session_id: str
    start_ms: int      # Unix timestamp начала сессии (ms)
    end_ms: Optional[int] = None  # Unix timestamp конца сессии (ms)
    total_duration_ms: int = 0    # общая длительность аудио в сессии


@dataclass
class StoreSession:
    """Состояние streaming сессии для магазина."""
    store_id: str
    sequence_id: str
    triton_client: Optional[InferenceServerClient] = None
    user_data: Optional[object] = None
    active: bool = False
    speaker_ids: Set[int] = field(default_factory=set)
    seller_ids: Set[str] = field(default_factory=set)  # Уникальные seller_ids для этого магазина
    total_chunks: int = 0
    last_activity: float = 0.0
    
    # Временная шкала для пересчёта относительного времени в абсолютное
    vad_sessions: List[VADSession] = field(default_factory=list)  # VAD сессии в порядке поступления
    cumulative_audio_ms: int = 0  # накопленная длительность аудио (без gaps)
    last_vad_end_ms: Optional[int] = None  # конец последней VAD сессии (Unix timestamp)
    
    # Результаты диаризации с абсолютным временем
    absolute_speaker_segments: List[dict] = field(default_factory=list)
    
    # Буфер для накопления аудио чанков перед отправкой в модель
    audio_buffer: List[tuple] = field(default_factory=list)  # List of (vad_session_id, chunk_id, audio_array, is_end)
    buffered_duration_ms: int = 0  # Общая длительность накопленного аудио в буфере
    last_buffer_send_time: float = 0  # Время последней отправки буфера


def calculate_chunk_duration_ms(size_bytes: int) -> int:
    """Вычисляет длительность чанка в миллисекундах."""
    return size_bytes * 1000 // (SAMPLE_RATE * BYTES_PER_SAMPLE)


def relative_to_absolute_time(session: 'StoreSession', relative_ms: int) -> int:
    """
    Convert relative time (from concatenated audio without gaps) to absolute Unix timestamp.
    
    The diarization model receives concatenated audio from multiple VAD sessions without
    the silence gaps between them. This function maps the relative time back to the
    absolute timeline by finding which VAD session contains the given relative time.
    
    Args:
        session: StoreSession containing VAD sessions in chronological order
        relative_ms: Time relative to start of concatenated audio (no gaps)
    
    Returns:
        Absolute Unix timestamp in milliseconds
    
    Example:
        # VAD session 1: 10:00:00-10:00:02 (2 sec audio)
        # Gap: 3 sec silence
        # VAD session 2: 10:00:05-10:00:06 (1 sec audio)
        # 
        # Diarization model sees: 0-3 sec (concatenated, no gap)
        # relative_ms=2.5 maps to absolute time 10:00:05.5 (in second VAD session)
    """
    if not session.vad_sessions:
        logger.warning("No VAD sessions found for time conversion")
        return 0
    
    cumulative_ms = 0
    
    for vad_session in session.vad_sessions:
        session_duration = vad_session.total_duration_ms
        
        if relative_ms < cumulative_ms + session_duration:
            # relative_ms falls within this VAD session
            offset_in_session = relative_ms - cumulative_ms
            return vad_session.start_ms + offset_in_session
        
        cumulative_ms += session_duration
    
    # relative_ms is beyond all sessions (shouldn't happen in normal operation)
    # Return end of last session (use start_ms + total_duration_ms since end_ms may be None)
    last_vad = session.vad_sessions[-1] if session.vad_sessions else None
    if last_vad:
        return last_vad.start_ms + last_vad.total_duration_ms
    return 0


class StreamingDiarizationWorker:
    """Worker для потоковой диаризации с накоплением спикеров по магазинам."""

    def __init__(self):
        # S3 configuration
        self.s3_endpoint = os.getenv("S3_ENDPOINT_URL", "http://minio:9000")
        self.s3_key = os.getenv("S3_ACCESS_KEY_ID", "minioadmin")
        self.s3_secret = os.getenv("S3_SECRET_ACCESS_KEY", "minioadmin")
        self.s3_bucket = os.getenv("S3_BUCKET", "audio-sessions")

        # Kafka configuration
        self.kafka_bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "redpanda:9092")
        self.kafka_topic = os.getenv("KAFKA_TOPIC", "audio_events")
        self.kafka_group = os.getenv("KAFKA_GROUP", "streaming-diarization-worker-v1")

        # Triton configuration
        self.triton_url = os.getenv("TRITON_URL", "server_triton-speech-segmentation:8001")
        self.model_name = os.getenv("TRITON_MODEL", "diarization")

        # Database configuration
        self.db_dsn = os.getenv("DATABASE_URL", "postgresql://speech:speech@postgres:5432/streaming_diarization")
        self.db_pool = None

        # Session management
        self.store_sessions: Dict[str, StoreSession] = {}
        self.session_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

        # Per-store audio buffers (accumulate chunks per store)
        self.store_audio_buffers: Dict[str, List[tuple]] = defaultdict(list)

        # S3 client
        self.s3_client = boto3.client(
            's3',
            endpoint_url=self.s3_endpoint,
            aws_access_key_id=self.s3_key,
            aws_secret_access_key=self.s3_secret,
            config=Config(s3={'addressing_style': 'path'})
        )

        # Statistics
        self.stats = {
            'total_events': 0,
            'total_chunks_processed': 0,
            'total_speakers_detected': 0,
            'stores_active': 0
        }

    async def get_s3_object(self, key: str) -> bytes:
        """Асинхронное чтение объекта из S3."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.s3_client.get_object(Bucket=self.s3_bucket, Key=key)['Body'].read()
        )

    def _get_store_id_from_session(self, session_id: str) -> str:
        """Извлекает store_id из session_id.
        
        Format: YYYYmmdd-HHMMSS-STORE_ID-SELLER_ID-UUID
        """
        try:
            parts = session_id.split('-')
            if len(parts) >= 4:
                return parts[2]  # store_id is the 3rd part
        except Exception:
            pass
        return "unknown"

    def _create_vad_marks(self) -> bytes:
        """Создаёт vad_marks protobuf message для streaming."""
        from diarization_protocol import output_pb2
        
        vad_marks = output_pb2.SpeakerSegment()
        
        # BEGIN mark at 0
        begin_mark = output_pb2.VoiceActivityMark()
        begin_mark.mark_type = output_pb2.VoiceActivityMark.VA_MARK_BEGIN
        begin_mark.offset_ms = 0
        
        # END mark at end (placeholder, will be updated)
        end_mark = output_pb2.VoiceActivityMark()
        end_mark.mark_type = output_pb2.VoiceActivityMark.VA_MARK_END
        end_mark.offset_ms = 1000  # Default 1 second, will be adjusted
        
        vad_marks.va_marks.extend([begin_mark, end_mark])
        
        return vad_marks.SerializeToString()

    class StreamingUserData:
        """Thread-safe container для responses с callback."""
        def __init__(self, worker: 'StreamingDiarizationWorker', store_id: str):
            self._completed_requests = Queue()
            self.worker = worker
            self.store_id = store_id
        
        def callback(self, result=None, error=None):
            """Callback для streaming responses."""
            if error:
                self._completed_requests.put(("error", error))
            else:
                self._completed_requests.put(("result", result))

    async def _update_timeline(self, store_id: str, vad_session_id: str, chunk_id: int,
                                timestamp_ms: int, duration_ms: int, is_end: bool):
        """
        Updates the timeline for store_id.
        
        Uses session_id to determine the actual start time of each VAD session,
        accounting for gaps between sessions.
        
        Args:
            store_id: store identifier
            vad_session_id: VAD session ID (contains date and time: YYYYmmdd-HHMMSS-...)
            chunk_id: chunk number within VAD session
            timestamp_ms: Unix timestamp from Kafka event (time of processing, not audio start)
            duration_ms: chunk duration in ms
            is_end: flag indicating end of VAD session
        """
        # Initialize StoreSession if it doesn't exist
        if store_id not in self.store_sessions:
            self.store_sessions[store_id] = StoreSession(
                store_id=store_id,
                sequence_id="",
                active=True
            )
        
        session = self.store_sessions[store_id]
        
        # Check if this chunk is the start of a new VAD session
        if chunk_id == 1:
            # New VAD session - extract start time from session_id
            try:
                meta = parse_session_id(vad_session_id)
                vad_start_ms = hhmmss_to_unix_ms(meta['date'], meta['time'])
                # Add seller_id to session's seller_ids set
                seller_id = meta.get('seller_id')
                if seller_id:
                    session.seller_ids.add(seller_id)
            except (ValueError, KeyError) as e:
                logger.warning(
                    "Failed to parse session_id %s: %s. Using timestamp_ms as fallback.",
                    vad_session_id, e
                )
                vad_start_ms = timestamp_ms
            
            vad_session = VADSession(
                session_id=vad_session_id,
                start_ms=vad_start_ms,
                total_duration_ms=duration_ms
            )
            session.vad_sessions.append(vad_session)
            
            # Log gap between VAD sessions
            if session.last_vad_end_ms is not None:
                gap_ms = vad_start_ms - session.last_vad_end_ms
                if gap_ms > 1000:  # Log gaps > 1 second
                    logger.info(
                        "Store %s: VAD session gap detected: %d ms (%.2f sec)",
                        store_id, gap_ms, gap_ms / 1000.0
                    )
            
            session.cumulative_audio_ms += duration_ms
            session.last_vad_end_ms = vad_start_ms + duration_ms
            
        else:
            # Continuation of existing VAD session
            if session.vad_sessions:
                last_vad = session.vad_sessions[-1]
                last_vad.total_duration_ms += duration_ms
        
        if is_end:
            # VAD session completed
            if session.vad_sessions:
                last_vad = session.vad_sessions[-1]
                last_vad.end_ms = last_vad.start_ms + last_vad.total_duration_ms
                logger.info(
                    "Store %s: VAD session %s completed (duration: %d ms, chunks: %d)",
                    store_id, vad_session_id,
                    last_vad.total_duration_ms,
                    chunk_id
                )

    async def _ensure_store_session(self, store_id: str):
        """Обеспечивает наличие активной сессии для магазина."""
        if store_id in self.store_sessions:
            session = self.store_sessions[store_id]
            if session.active and session.triton_client:
                return session
        
        # Create new Triton streaming session
        sequence_id = str(uuid.uuid4())
        
        # Thread-safe container for responses
        user_data = self.StreamingUserData(self, store_id)
        
        # Create Triton client
        client = InferenceServerClient(url=self.triton_url)
        
        # Start streaming
        client.start_stream(callback=user_data.callback)
        
        # Обновляем существующую StoreSession или создаём новую
        if store_id in self.store_sessions:
            session = self.store_sessions[store_id]
            session.triton_client = client
            session.sequence_id = sequence_id
            session.user_data = user_data
            session.active = True
        else:
            session = StoreSession(
                store_id=store_id,
                sequence_id=sequence_id,
                triton_client=client,
                user_data=user_data,
                active=True,
                speaker_ids=set(),
                total_chunks=0,
                last_activity=time.time()
            )
            self.store_sessions[store_id] = session
        
        logger.info("Created/Updated streaming session for store %s (sequence_id=%s)", store_id, sequence_id)
        
        return session

    async def _send_buffered_audio_to_streaming(self, session: StoreSession):
        """Отправляет накопленное аудио из буфера в streaming сессию."""
        if not session.audio_buffer:
            return
        
        # Concatenate all audio chunks in buffer
        concatenated_audio = np.concatenate([chunk[2] for chunk in session.audio_buffer])
        total_chunks_in_buffer = len(session.audio_buffer)
        is_end = session.audio_buffer[-1][3]  # is_end from last chunk
        
        logger.info(
            "Store %s: Sending buffered audio to diarization (%d chunks, %d ms total)",
            session.store_id, total_chunks_in_buffer, session.buffered_duration_ms
        )
        
        vad_marks = self._create_vad_marks()
        vad_marks_np = np.array([[vad_marks]], dtype=object)
        
        # Prepare input
        input_np = np.zeros((1, len(concatenated_audio)), dtype=np.int16)
        input_np[0][:len(concatenated_audio)] = concatenated_audio[:len(concatenated_audio)]
        
        inputs = [
            InferInput("input", input_np.shape, np_to_triton_dtype(input_np.dtype)),
            InferInput("vad_marks", vad_marks_np.shape, "BYTES"),
        ]
        inputs[0].set_data_from_numpy(input_np)
        inputs[1].set_data_from_numpy(vad_marks_np)
        
        outputs = [InferRequestedOutput("DiarisationResponse")]
        
        parameters = {
            "sample_rate": "16000",
            "window_fs": "1.52",
            "period_fs": "0.7",
            "frame_shift": "10",
            "max_speaker": "20",
            "num_speaker": "0",
            "use_audiogram_vad": "False"
        }
        
        # Send async stream infer
        session.triton_client.async_stream_infer(
            self.model_name,
            inputs,
            request_id=f"{session.store_id}-buffered-{session.total_chunks}",
            outputs=outputs,
            parameters=parameters,
            sequence_id=session.sequence_id,
            sequence_start=(session.total_chunks == 0),
            sequence_end=is_end,
            enable_empty_final_response=True
        )
        
        session.total_chunks += total_chunks_in_buffer
        session.last_activity = time.time()

    async def _send_chunk_to_streaming(self, session: StoreSession, audio_data: np.ndarray, chunk_id: int, is_end: bool):
        """Отправляет чанк в streaming сессию (legacy, kept for compatibility)."""
        vad_marks = self._create_vad_marks()
        vad_marks_np = np.array([[vad_marks]], dtype=object)
        
        # Prepare input
        input_np = np.zeros((1, len(audio_data)), dtype=np.int16)
        input_np[0][:len(audio_data)] = audio_data[:len(audio_data)]
        
        inputs = [
            InferInput("input", input_np.shape, np_to_triton_dtype(input_np.dtype)),
            InferInput("vad_marks", vad_marks_np.shape, "BYTES"),
        ]
        inputs[0].set_data_from_numpy(input_np)
        inputs[1].set_data_from_numpy(vad_marks_np)
        
        outputs = [InferRequestedOutput("DiarisationResponse")]
        
        parameters = {
            "sample_rate": "16000",
            "window_fs": "1.52",
            "period_fs": "0.7",
            "frame_shift": "10",
            "max_speaker": "20",
            "num_speaker": "0",
            "use_audiogram_vad": "False"
        }
        
        # Send async stream infer
        session.triton_client.async_stream_infer(
            self.model_name,
            inputs,
            request_id=f"{session.store_id}-{chunk_id}",
            outputs=outputs,
            parameters=parameters,
            sequence_id=session.sequence_id,
            sequence_start=(session.total_chunks == 0),
            sequence_end=is_end,
            enable_empty_final_response=True
        )
        
        session.total_chunks += 1
        session.last_activity = time.time()
        
        logger.debug("Sent chunk %d to store %s (total: %d, is_end: %s)", 
                     chunk_id, session.store_id, session.total_chunks, is_end)

    async def _check_for_responses(self, session: StoreSession, timeout: float = 1.0):
        """Проверяет наличие ответов от Triton."""
        if not session.user_data or not session.active:
            return
        
        try:
            while True:
                msg_type, msg = session.user_data._completed_requests.get(timeout=0.1)
                
                if msg_type == "error":
                    logger.error("Inference error for store %s: %s", session.store_id, msg)
                    continue
                
                elif msg_type == "result":
                    if msg is None:
                        continue
                    
                    try:
                        output_np = msg.as_numpy("DiarisationResponse")
                        if output_np is not None and output_np.size > 0:
                            # Parse response
                            diarization_bytes = output_np.tolist()
                            from diarization_protocol import output_pb2
                            diarization_result = output_pb2.DiarisationResponse.FromString(diarization_bytes)
                            
                            # Обновляем tracking спикеров и сохраняем сегменты с абсолютным временем
                            new_speakers = []
                            for speaker_segment in diarization_result.speaker_segment:
                                if len(speaker_segment.va_marks) >= 2:
                                    speaker_id = speaker_segment.speaker_id
                                    relative_start_ms = speaker_segment.va_marks[0].offset_ms
                                    relative_end_ms = speaker_segment.va_marks[1].offset_ms
                                    
                                    # Конвертируем в абсолютное время
                                    absolute_start_ms = relative_to_absolute_time(session, relative_start_ms)
                                    absolute_end_ms = relative_to_absolute_time(session, relative_end_ms)
                                    
                                    # Сохраняем сегмент с абсолютным временем
                                    segment_data = {
                                        'speaker_id': speaker_id,
                                        'absolute_start_ms': absolute_start_ms,
                                        'absolute_end_ms': absolute_end_ms,
                                        'relative_start_ms': relative_start_ms,
                                        'relative_end_ms': relative_end_ms
                                    }
                                    session.absolute_speaker_segments.append(segment_data)
                                    logger.debug(
                                        "Store %s: Segment speaker=%d rel=%d-%d abs=%d-%d",
                                        session.store_id, speaker_id,
                                        relative_start_ms, relative_end_ms,
                                        absolute_start_ms, absolute_end_ms
                                    )
                                    
                                if speaker_id not in session.speaker_ids:
                                    session.speaker_ids.add(speaker_id)
                                    new_speakers.append(speaker_id)
                            
                            if new_speakers:
                                logger.info(
                                    "Store %s: New speakers detected %s (total: %d)",
                                    session.store_id, new_speakers, len(session.speaker_ids)
                                )
                            
                            self.stats['total_speakers_detected'] += len(new_speakers)
                            
                            # Save all segments to database immediately
                            if session.absolute_speaker_segments:
                                logger.info("About to save %d segments for store %s", len(session.absolute_speaker_segments), session.store_id)
                                logger.info("First segment: %s", session.absolute_speaker_segments[0] if session.absolute_speaker_segments else "None")
                                sellers_list = list(session.seller_ids) if session.seller_ids else None
                                await self.save_diarization_result(session.store_id, session.absolute_speaker_segments, sellers_list)
                    
                    except Exception as e:
                        logger.debug("Failed to parse response: %s", e)
                        continue
                         
        except Exception:
            pass  # No more responses available

    async def handle_event(self, event: dict):
        """Обработчик Kafka событий."""
        self.stats['total_events'] += 1
        
        # Filter by event type
        if event.get("type") != "audio_chunk_saved":
            return

        session_id = event.get("session_id")
        timestamp_ms = event.get("timestamp_ms")  # ← Unix timestamp (абсолютное время)
        payload = event.get("payload", {})

        chunk_id = payload.get("chunk_id")
        s3_key = payload.get("s3_key")
        size_bytes = payload.get("size_bytes", 0)
        is_end = payload.get("is_end", False)

        if not session_id or chunk_id is None or not s3_key or not timestamp_ms:
            logger.warning("Invalid event: missing required fields")
            return

        # Extract store_id
        store_id = self._get_store_id_from_session(session_id)

        async with self.session_locks[store_id]:
            # Вычисляем длительность чанка
            duration_ms = calculate_chunk_duration_ms(size_bytes)
            
            # Обновляем временную шкалу для store_id
            await self._update_timeline(store_id, session_id, chunk_id, timestamp_ms, duration_ms, is_end)
            
            # Download chunk
            wav_bytes = await self.get_s3_object(s3_key)
            
            # Parse WAV
            import io
            import wave
            with wave.open(io.BytesIO(wav_bytes), 'rb') as wf:
                pcm_data = wf.readframes(wf.getnframes())
            
            audio_array = np.frombuffer(pcm_data, dtype=np.int16)
            
            # Ensure session exists
            session = await self._ensure_store_session(store_id)
            
            # Add to buffer
            session.audio_buffer.append((session_id, chunk_id, audio_array, is_end))
            session.buffered_duration_ms += duration_ms
            self.stats['total_chunks_processed'] += 1
            
            # Check if we have enough audio to send to model
            should_send = False
            if session.buffered_duration_ms >= MIN_AUDIO_DURATION_FOR_DIARIZATION_MS:
                # Send when buffer reaches minimum duration
                should_send = True
            elif time.time() - session.last_buffer_send_time > BUFFER_TIMEOUT_SECONDS:
                # Send if buffer timeout exceeded (even if short)
                should_send = True
                logger.debug(
                    "Store %s: Buffer timeout exceeded (%.1f sec), sending %d ms of audio",
                    store_id, time.time() - session.last_buffer_send_time, session.buffered_duration_ms
                )
            
            if should_send and session.audio_buffer:
                # Send buffered audio to streaming
                await self._send_buffered_audio_to_streaming(session)
                # Clear buffer after sending
                session.audio_buffer.clear()
                session.buffered_duration_ms = 0
                session.last_buffer_send_time = time.time()
            
            # Check for responses periodically
            await self._check_for_responses(session)
            
            # Log every 100 chunks
            if self.stats['total_chunks_processed'] % 100 == 0:
                logger.info(
                    "Stats: events=%d chunks=%d speakers=%d stores=%d",
                    self.stats['total_events'],
                    self.stats['total_chunks_processed'],
                    self.stats['total_speakers_detected'],
                    len(self.store_sessions)
                )

    def _log_store_summary(self):
        """Логирование сводки по всем магазинам."""
        logger.info("\n" + "=" * 70)
        logger.info("STORE DIARIZATION SUMMARY")
        logger.info("=" * 70)
        
        for store_id, session in sorted(self.store_sessions.items()):
            if session.active and session.total_chunks > 0:
                logger.info(
                    "Store: %s | Chunks: %d | Speakers: %d | Speaker IDs: %s",
                    store_id, session.total_chunks, len(session.speaker_ids),
                    sorted(session.speaker_ids)
                )
        
        logger.info("-" * 70)
        logger.info(
            "Total: stores=%d chunks=%d speakers=%d",
            len(self.store_sessions),
            self.stats['total_chunks_processed'],
            self.stats['total_speakers_detected']
        )
        logger.info("=" * 70 + "\n")

    async def start(self):
        """Initialize database pool."""
        self.db_pool = await asyncpg.create_pool(self.db_dsn)
        logger.info("Database pool initialized for streaming diarization")

    async def stop(self):
        """Close database pool."""
        if self.db_pool:
            await self.db_pool.close()
            logger.info("Database pool closed")

    async def save_diarization_result(self, store_id: str, segments: List[dict], sellers: List[str] = None):
        """
        Save diarization result to database with absolute timestamps.
        
        Args:
            store_id: Store identifier
            segments: List of speaker segments with absolute timestamps
            sellers: List of seller_ids active during this session
        """
        if not self.db_pool:
            logger.warning("Database pool not initialized, skipping save")
            return
        
        try:
            # Generate session_id: YYYY-MM-DD_STORE_ID
            today = datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d")
            session_id = f"{today}_{store_id}"
            
            # Format segments for storage
            speakers_json = json.dumps(segments, ensure_ascii=False)
            sellers_json = json.dumps(sellers or [], ensure_ascii=False)
            
            logger.info("Saving diarization result for session %s (%d segments)", session_id, len(segments))
            
            async with self.db_pool.acquire() as conn:
                result = await conn.execute(
                    """
                    INSERT INTO diarization_results (session_id, store_id, sellers, speakers, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, NOW(), NOW())
                    ON CONFLICT (session_id)
                    DO UPDATE
                    SET speakers = EXCLUDED.speakers,
                        sellers = EXCLUDED.sellers,
                        updated_at = NOW();
                    """,
                    session_id,
                    store_id,
                    sellers_json,
                    speakers_json
                )
                logger.info("Saved diarization result for session %s (%d segments)", session_id, len(segments))
        except Exception as e:
            logger.exception("Failed to save diarization result for store %s: %s", store_id, e)

    async def run(self):
        """Запуск worker'а."""
        logger.info("Starting Streaming Diarization Worker")
        logger.info("Kafka: %s, topic: %s", self.kafka_bootstrap, self.kafka_topic)
        logger.info("Triton: %s, model: %s", self.triton_url, self.model_name)
        logger.info("S3: %s, bucket: %s", self.s3_endpoint, self.s3_bucket)
        logger.info("Database: %s", self.db_dsn)

        # Initialize database pool
        await self.start()

        consumer = AIOKafkaConsumer(
            self.kafka_topic,
            bootstrap_servers=self.kafka_bootstrap,
            group_id=self.kafka_group,
            auto_offset_reset='earliest',
            enable_auto_commit=True,
            auto_commit_interval_ms=5000
        )

        await consumer.start()
        logger.info("🚀 Streaming Diarization Worker started")

        # Summary timer
        last_summary_time = time.time()
        summary_interval = 60  # seconds

        try:
            async for msg in consumer:
                try:
                    event = json.loads(msg.value.decode('utf-8'))
                    await self.handle_event(event)
                    
                    # Periodic summary
                    if time.time() - last_summary_time > summary_interval:
                        self._log_store_summary()
                        last_summary_time = time.time()
                        
                except json.JSONDecodeError:
                    logger.error("Failed to decode Kafka message")
                except Exception as e:
                    logger.exception("Failed to process Kafka message: %s", str(e))
        finally:
            # Cleanup
            logger.info("Shutting down streaming sessions...")
            
            for store_id, session in self.store_sessions.items():
                if session.triton_client:
                    session.triton_client.stop_stream()
                    session.triton_client.close()
            
            self._log_store_summary()
            
            await consumer.stop()
            await self.stop()
            logger.info("Streaming Diarization Worker stopped")


def configure_logging():
    """Настройка логирования."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout
    )


async def main():
    """Main entry point."""
    configure_logging()

    worker = StreamingDiarizationWorker()
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
