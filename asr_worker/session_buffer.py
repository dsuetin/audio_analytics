from collections import defaultdict
from dataclasses import dataclass, field
import asyncio
import time


# ASR adaptive batching constants
ASR_SAMPLE_RATE = 16000
ASR_CHUNK_MS = 160
ASR_BLOCK_SIZE_SAMPLES = ASR_SAMPLE_RATE * ASR_CHUNK_MS // 1000  # 2560
ASR_BLOCK_SIZE_BYTES = ASR_BLOCK_SIZE_SAMPLES * 2  # 5120
ASR_MAX_BATCH_CHUNKS = 93  # 93 × 160ms = 14.88s
ASR_MAX_BATCH_BYTES = ASR_MAX_BATCH_CHUNKS * ASR_BLOCK_SIZE_BYTES  # 476160


@dataclass
class BufferState:
    data: bytearray = field(default_factory=bytearray)

    cursor: int = 0

    # порядок чанков
    expected_chunk_id: int = 1
    pending: dict[int, bytes] = field(default_factory=dict)

    # финализация
    last_chunk_id: int | None = None

    # для таймаутов в будущем
    last_activity_ts: float = field(default_factory=time.time)
    
    # ждём отсутствующий chunk
    waiting_since_ts: float | None = None

class SessionBuffer:
    def __init__(
        self,
        sample_rate: int = 16000,
        sample_width: int = 2,
    ):
        self.buf = defaultdict(BufferState)
        self.locks = defaultdict(asyncio.Lock)

        self.bytes_per_ms = (
            sample_rate * sample_width
        ) / 1000

    async def add(
        self,
        session_id: str,
        chunk_id: int,
        data: bytes,
        is_end: bool = False,
    ):
        async with self.locks[session_id]:
            state = self.buf[session_id]

            state.last_activity_ts = time.time()

            # запоминаем номер последнего чанка
            if is_end:
                state.last_chunk_id = chunk_id

            # дубликат уже обработанного чанка
            if chunk_id < state.expected_chunk_id:
                return

            # дубликат в pending
            if chunk_id in state.pending:
                return

            # пришёл не тот чанк который ждём
            if chunk_id > state.expected_chunk_id:
                state.pending[chunk_id] = data
                return

            # chunk_id == expected_chunk_id
            state.data.extend(data)
            state.expected_chunk_id += 1

            recovered = False

            # подтягиваем накопившиеся чанки
            while state.expected_chunk_id in state.pending:
                next_chunk = state.pending.pop(
                    state.expected_chunk_id
                )

                state.data.extend(next_chunk)
                state.expected_chunk_id += 1
                recovered = True

        # дырка закрылась
        if recovered:
            state.waiting_since_ts = None


    async def is_stalled(
        self,
        session_id: str,
        timeout_sec: float = 5.0,
    ) -> bool:
        async with self.locks[session_id]:
            state = self.buf.get(session_id)

            if state is None:
                return False

            return (
                state.last_chunk_id is not None
                and state.waiting_since_ts is not None
                and (
                    time.time() - state.waiting_since_ts
                ) > timeout_sec
            )
        
        
    async def pop_if_ready(
        self,
        session_id: str,
        min_ms: int = 160,
    ) -> bytes | None:
        async with self.locks[session_id]:
            state = self.buf[session_id]

            available = len(state.data) - state.cursor

            target_bytes = int(
                min_ms * self.bytes_per_ms
            )

            # выравнивание по int16
            target_bytes = (target_bytes // 2) * 2

            if available < target_bytes:
                return None

            chunk = bytes(
                state.data[
                    state.cursor : state.cursor + target_bytes
                ]
            )

            state.cursor += target_bytes

            # периодически освобождаем память
            if state.cursor > 1024 * 1024:
                state.data = state.data[state.cursor :]
                state.cursor = 0

            return chunk

    async def extract_batch(self, session_id: str) -> bytes | None:
        """Extract max possible batch with adaptive batching.
        
        Returns:
            bytes of PCM samples (5120 to 476160 bytes = 1 to 93 chunks),
            or None if no full 160ms chunk available.
            Data stays in buffer until caller confirms via commit().
        """
        async with self.locks[session_id]:
            state = self.buf[session_id]
            available = len(state.data) - state.cursor

            # Need at least one full 160ms chunk
            if available < ASR_BLOCK_SIZE_BYTES:
                return None

            # Count full chunks available
            chunks_available = available // ASR_BLOCK_SIZE_BYTES
            chunks_to_send = min(chunks_available, ASR_MAX_BATCH_CHUNKS)
            bytes_to_send = chunks_to_send * ASR_BLOCK_SIZE_BYTES

            batch = bytes(
                state.data[state.cursor : state.cursor + bytes_to_send]
            )
            return batch

    async def commit(self, session_id: str, bytes_count: int) -> None:
        """Remove successfully sent samples from buffer.
        
        Must be called AFTER gRPC send succeeds.
        If send fails, do NOT call commit() - samples remain for retry.
        
        Args:
            session_id: Session identifier
            bytes_count: Number of bytes successfully sent
        """
        async with self.locks[session_id]:
            state = self.buf[session_id]
            if bytes_count > 0 and bytes_count <= len(state.data) - state.cursor:
                state.cursor += bytes_count

                # периодически освобождаем память
                if state.cursor > 1024 * 1024:
                    state.data = state.data[state.cursor :]
                    state.cursor = 0

    async def is_end_ready(
        self,
        session_id: str,
    ) -> bool:
        async with self.locks[session_id]:
            state = self.buf[session_id]

            return (
                state.last_chunk_id is not None
                and state.expected_chunk_id
                > state.last_chunk_id
            )

    async def pop_all(
        self,
        session_id: str,
    ) -> bytes:
        async with self.locks[session_id]:
            state = self.buf[session_id]

            remaining = bytes(
                state.data[state.cursor :]
            )

            del self.buf[session_id]

            return remaining