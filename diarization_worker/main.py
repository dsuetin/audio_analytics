"""Diarization Worker — оффлайн-диаризация аудиосегментов.

Сервис выполняет:
1. Подписку на Kafka topic audio_events
2. Сборку всех чанков сессии после получения is_end=True
3. Запрос к Triton diarization модели
4. Вывод результата в структурированных логах
"""

import asyncio
import json
import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional

import asyncpg
import boto3
from aiokafka import AIOKafkaConsumer
from botocore.config import Config

logger = logging.getLogger(__name__)


@dataclass
class ChunkInfo:
    """Информация о чанке аудио."""
    chunk_id: int
    s3_key: str
    size_bytes: int


class DiarizationWorker:
    """Worker для оффлайн-диаризации аудиосессий."""

    def __init__(self):
        # S3 configuration
        self.s3_endpoint = os.getenv("S3_ENDPOINT_URL", "http://minio:9000")
        self.s3_key = os.getenv("S3_ACCESS_KEY_ID", "minioadmin")
        self.s3_secret = os.getenv("S3_SECRET_ACCESS_KEY", "minioadmin")
        self.s3_bucket = os.getenv("S3_BUCKET", "audio-sessions")

        # Kafka configuration
        self.kafka_bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "redpanda:9092")
        self.kafka_topic = os.getenv("KAFKA_TOPIC", "audio_events")
        self.kafka_group = os.getenv("KAFKA_GROUP", "diarization-worker-v1")

        # Triton configuration
        self.triton_url = os.getenv("TRITON_URL", "server_triton-speech-segmentation:8001")

        # Database configuration
        self.db_dsn = os.getenv("DATABASE_URL", "postgresql://speech:speech@postgres:5432/speech_db")
        self.db_pool: asyncpg.Pool | None = None

        # Session buffers
        self.session_chunks: Dict[str, Dict[int, ChunkInfo]] = defaultdict(dict)
        self.session_end_flags: Dict[str, bool] = defaultdict(bool)
        self.session_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

        # S3 client (sync, will use in thread pool)
        self.s3_client = boto3.client(
            's3',
            endpoint_url=self.s3_endpoint,
            aws_access_key_id=self.s3_key,
            aws_secret_access_key=self.s3_secret,
            config=Config(s3={'addressing_style': 'path'})
        )

        # Processed sessions tracking (to avoid reprocessing)
        self.processed_sessions: set = set()

    async def start(self):
        """Инициализация пула соединений с БД."""
        self.db_pool = await asyncpg.create_pool(self.db_dsn)
        logger.info("Database pool initialized")

    async def stop(self):
        """Закрытие пула соединений с БД."""
        if self.db_pool:
            await self.db_pool.close()
            logger.info("Database pool closed")

    async def save_diarization_result(self, session_id: str, result: dict):
        """Сохранение результата диаризации в БД."""
        if not self.db_pool:
            logger.warning("Database pool not initialized, skipping save")
            return

        try:
            meta = self._parse_session_id(session_id)
            store_id = meta.get('store_id')
            seller_id = meta.get('seller_id')
            
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO diarization_results (session_id, store_id, seller_id, speakers)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (session_id)
                    DO UPDATE
                    SET speakers = EXCLUDED.speakers;
                    """,
                    session_id,
                    store_id,
                    seller_id,
                    json.dumps(result.get('segments', [])),
                )
            logger.debug("Saved diarization result for session: %s", session_id)
        except Exception as e:
            logger.exception("Failed to save diarization result for session %s: %s", session_id, e)

    async def get_s3_object(self, key: str) -> bytes:
        """Асинхронное чтение объекта из S3."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.s3_client.get_object(Bucket=self.s3_bucket, Key=key)['Body'].read()
        )

    async def handle_event(self, event: dict):
        """Обработчик Kafka событий."""
        # Filter by event type
        if event.get("type") != "audio_chunk_saved":
            return

        session_id = event.get("session_id")
        payload = event.get("payload", {})

        chunk_id = payload.get("chunk_id")
        s3_key = payload.get("s3_key")
        size_bytes = payload.get("size_bytes", 0)
        is_end = payload.get("is_end", False)

        if not session_id or chunk_id is None or not s3_key:
            logger.warning("Invalid event: missing required fields")
            return

        async with self.session_locks[session_id]:
            # Store chunk info
            self.session_chunks[session_id][chunk_id] = ChunkInfo(
                chunk_id=chunk_id,
                s3_key=s3_key,
                size_bytes=size_bytes
            )

            # Mark session as ended if this is the final chunk
            if is_end:
                self.session_end_flags[session_id] = True
                logger.info(
                    "Session completed: session_id=%s total_chunks=%d",
                    session_id,
                    len(self.session_chunks[session_id])
                )

                # Check if we can process (all chunks received)
                await self._try_process_session(session_id)

    async def _try_process_session(self, session_id: str):
        """Попытка обработать сессию если все чанки собраны."""
        # Check if already processed
        if session_id in self.processed_sessions:
            logger.debug("Session already processed: %s", session_id)
            return

        # Check if session is marked as ended
        if not self.session_end_flags.get(session_id):
            logger.debug("Session not ended yet: %s", session_id)
            return

        # Get all chunks for this session
        chunks = self.session_chunks.get(session_id, {})
        if not chunks:
            logger.warning("No chunks found for session: %s", session_id)
            return

        # Verify all chunks are present (1 to max chunk_id)
        max_chunk_id = max(chunks.keys())
        missing_chunks = [i for i in range(1, max_chunk_id + 1) if i not in chunks]

        if missing_chunks:
            logger.warning(
                "Missing chunks for session %s: missing=%s total=%d",
                session_id,
                missing_chunks,
                len(chunks)
            )
            # TODO: Implement retry logic with timeout
            return

        # Process the session
        await self._process_session(session_id, chunks)

    async def _process_session(self, session_id: str, chunks: Dict[int, ChunkInfo]):
        """Обработка сессии: сборка аудио и вызов диаризации."""
        logger.info("Processing session: %s chunks=%d", session_id, len(chunks))

        start_time = time.time()

        try:
            # Sort chunks by chunk_id
            sorted_chunks = sorted(chunks.values(), key=lambda c: c.chunk_id)

            # Download and concatenate all chunks
            audio_data = await self._download_and_concat_chunks(sorted_chunks)

            if not audio_data:
                logger.error("Failed to download audio chunks for session: %s", session_id)
                return

            # Call diarization model
            diarization_result = await self._call_diarization(session_id, audio_data)

            # Format and log result
            processing_time = time.time() - start_time
            self._log_diarization_result(session_id, diarization_result, processing_time, len(audio_data))

            # Save to database
            await self.save_diarization_result(session_id, diarization_result)

            # Mark as processed
            self.processed_sessions.add(session_id)

            # Cleanup session data
            self.session_chunks.pop(session_id, None)
            self.session_end_flags.pop(session_id, None)
            self.session_locks.pop(session_id, None)

        except Exception as e:
            logger.exception("Failed to process session %s: %s", session_id, str(e))

    async def _download_and_concat_chunks(self, chunks: List[ChunkInfo]) -> bytes:
        """Скачивание и конкатенация всех чанков в один аудиофайл."""
        logger.debug("Downloading %d chunks", len(chunks))

        download_start = time.time()

        # Download all chunks in parallel
        download_tasks = [self.get_s3_object(chunk.s3_key) for chunk in chunks]
        chunk_data_list = await asyncio.gather(*download_tasks)

        download_time = time.time() - download_start
        logger.debug("Downloaded all chunks in %.2fs", download_time)

        # Concatenate WAV files (extract PCM from each and combine)
        import io
        import wave

        all_pcm = b''

        for wav_bytes in chunk_data_list:
            with wave.open(io.BytesIO(wav_bytes), 'rb') as wf:
                pcm_data = wf.readframes(wf.getnframes())
                all_pcm += pcm_data

        logger.debug("Concatenated %d bytes of PCM data", len(all_pcm))

        return all_pcm

    async def _call_diarization(self, session_id: str, pcm_data: bytes):
        """Вызов модели диаризации в Triton."""
        logger.info("Calling diarization model for session: %s", session_id)

        # Import Triton client - use gRPC
        from tritonclient.grpc import InferenceServerClient, InferInput
        import numpy as np

        # Prepare audio data (INT16 PCM, 16000 Hz, mono)
        audio_array = np.frombuffer(pcm_data, dtype=np.int16)

        # Prepare vad_marks (single segment covering entire audio)
        vad_marks_data = self._create_vad_marks(len(audio_array), sample_rate=16000)

        # Debug logging
        logger.info(
            "Audio: samples=%d bytes=%d vad_marks_bytes=%d",
            len(audio_array), len(pcm_data), len(vad_marks_data)
        )

        # Model has max_batch_size: 1 with dynamic_batching
        # Both inputs need batch dimension
        client = InferenceServerClient(url=self.triton_url)

        # Audio: (1, num_samples) - explicit batch dimension
        input_audio = InferInput('input', (1, len(audio_array)), 'INT16')
        input_audio.set_data_from_numpy(audio_array.reshape(1, -1))

        # vad_marks: (1, 1) - batch dimension + 1D array with single element
        # Model expects [-1, -1] shape, accesses vad_marks[0][0] after .as_numpy()
        vad_array = np.array([[vad_marks_data]], dtype=object)
        input_vad = InferInput('vad_marks', (1, 1), 'BYTES')
        input_vad.set_data_from_numpy(vad_array)

        inputs = [input_audio, input_vad]

        # Call model
        infer_start = time.time()
        response = client.infer(
            model_name='diarization',
            inputs=inputs,
            request_id=session_id
        )
        infer_time = time.time() - infer_start
        logger.info("Diarization inference took %.2fs for session %s", infer_time, session_id)

        # Parse response - handle both list and array formats
        response_numpy = response.as_numpy('DiarisationResponse')
        logger.info("Response type: %s, shape: %s, ndim: %s", type(response_numpy), getattr(response_numpy, 'shape', 'N/A'), getattr(response_numpy, 'ndim', 'N/A'))

        # Handle different response formats - response can be scalar, 1D, or 2D array
        if isinstance(response_numpy, bytes):
            # Direct bytes response
            response_data = response_numpy
        elif isinstance(response_numpy, list):
            if len(response_numpy) > 0:
                response_data = response_numpy[0]
            else:
                logger.error("Empty response list from diarization model")
                return {}
        elif hasattr(response_numpy, 'ndim'):
            # numpy array
            if response_numpy.ndim == 0:
                # Scalar array - extract directly
                response_data = response_numpy.item()
            elif response_numpy.ndim == 1:
                # 1D array
                response_data = response_numpy[0]
            elif response_numpy.ndim >= 2:
                # 2D+ array
                response_data = response_numpy[0][0]
            else:
                logger.error("Unexpected array ndim: %s", response_numpy.ndim)
                return {}
        else:
            logger.error("Unexpected response type: %s", type(response_numpy))
            return {}

        logger.info("Extracted response_data type: %s, len: %s", type(response_data), len(response_data) if hasattr(response_data, '__len__') else 'N/A')

        return self._parse_diarization_response(response_data, len(audio_array), 16000)

    def _create_vad_marks(self, num_samples: int, sample_rate: int = 16000) -> bytes:
        """Создание vad_marks protobuf message."""
        # Import protobuf definitions
        try:
            from diarization_protocol import output_pb2
        except ImportError:
            logger.error("diarization_protocol not available")
            return b''

        vad_marks = output_pb2.SpeakerSegment()

        # Add begin mark
        begin_mark = output_pb2.VoiceActivityMark()
        begin_mark.mark_type = output_pb2.VoiceActivityMark.VA_MARK_BEGIN
        begin_mark.offset_ms = 0  # milliseconds

        # Add end mark (in milliseconds)
        end_mark = output_pb2.VoiceActivityMark()
        end_mark.mark_type = output_pb2.VoiceActivityMark.VA_MARK_END
        end_mark.offset_ms = int((num_samples / sample_rate) * 1000)  # milliseconds

        vad_marks.va_marks.extend([begin_mark, end_mark])

        return vad_marks.SerializeToString()

    def _parse_diarization_response(self, response_bytes: bytes, num_samples: int, sample_rate: int) -> dict:
        """Парсинг ответа модели диаризации."""
        try:
            from diarization_protocol import output_pb2
        except ImportError:
            logger.error("diarization_protocol not available")
            return {}

        response = output_pb2.DiarisationResponse.FromString(response_bytes)

        segments = []
        speaker_ids = set()

        for segment in response.speaker_segment:
            if len(segment.va_marks) >= 2:
                start_ms = segment.va_marks[0].offset_ms
                end_ms = segment.va_marks[1].offset_ms
                speaker_id = segment.speaker_id

                segments.append({
                    'speaker_id': speaker_id,
                    'start_ms': start_ms,
                    'end_ms': end_ms,
                    'start_sec': start_ms / 1000.0,
                    'end_sec': end_ms / 1000.0,
                    'duration_sec': (end_ms - start_ms) / 1000.0
                })

                speaker_ids.add(speaker_id)

        # Sort segments by start time
        segments.sort(key=lambda s: s['start_sec'])

        return {
            'segments': segments,
            'num_speakers': len(speaker_ids),
            'total_duration_sec': num_samples / sample_rate
        }

    def _log_diarization_result(self, session_id: str, result: dict, processing_time: float, audio_bytes: int):
        """Форматированный вывод результата диаризации."""
        if not result or 'segments' not in result:
            logger.warning("No diarization result for session: %s", session_id)
            return

        segments = result['segments']
        num_speakers = result.get('num_speakers', 0)
        total_duration = result.get('total_duration_sec', 0)

        # Parse session_id for metadata
        meta = self._parse_session_id(session_id)

        # Print formatted result
        print("\n" + "=" * 60)
        print("DIARIZATION RESULT")
        print("=" * 60)
        print(f"Dialog ID: {session_id}")
        if meta:
            print(f"Store ID: {meta.get('store_id', 'N/A')}")
            print(f"Seller ID: {meta.get('seller_id', 'N/A')}")
        print(f"Audio duration: {total_duration:.2f} sec")
        print(f"Audio size: {audio_bytes / 1024:.1f} KB")
        print(f"\nSpeakers detected: {num_speakers}")
        print()

        for seg in segments:
            start_str = f"{seg['start_sec']:06.3f}"
            end_str = f"{seg['end_sec']:06.3f}"
            print(f"SPEAKER_{seg['speaker_id']} | {start_str} - {end_str} ({seg['duration_sec']:.3f}s)")

        print()
        print(f"Total segments: {len(segments)}")
        print(f"Processing time: {processing_time:.2f} sec")
        print("=" * 60 + "\n")

    def _parse_session_id(self, session_id: str) -> dict:
        """Парсинг session_id для извлечения metadata."""
        # Format: YYYYmmdd-HHMMSS-STORE_ID-SELLER_ID-UUID
        try:
            parts = session_id.split('-')
            if len(parts) >= 4:
                return {
                    'date': parts[0],
                    'time': parts[1],
                    'store_id': parts[2],
                    'seller_id': parts[3]
                }
        except Exception:
            pass
        return {}

    async def run(self):
        """Запуск worker'а."""
        logger.info("Starting Diarization Worker")
        logger.info("Kafka: %s, topic: %s", self.kafka_bootstrap, self.kafka_topic)
        logger.info("Triton: %s", self.triton_url)
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
        logger.info("🚀 Diarization Worker started")

        try:
            async for msg in consumer:
                try:
                    event = json.loads(msg.value.decode('utf-8'))
                    await self.handle_event(event)
                except json.JSONDecodeError:
                    logger.error("Failed to decode Kafka message")
                except Exception as e:
                    logger.exception("Failed to process Kafka message: %s", str(e))
        finally:
            await consumer.stop()
            await self.stop()
            logger.info("Diarization Worker stopped")


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

    worker = DiarizationWorker()
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
