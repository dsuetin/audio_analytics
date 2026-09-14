"""Thread-safe audio FIFO buffer for adaptive batching.

Supports:
  - append from audio callback (150ms chunks = 2400 samples @ 16kHz)
  - atomic batch extraction (max 15s = 240000 samples, krate 150ms)
  - no audio loss: samples stay in buffer until successfully sent
"""

import threading
from typing import Optional, Tuple


SAMPLE_RATE = 16000
CHUNK_MS = 150
BLOCK_SIZE_SAMPLES = SAMPLE_RATE * CHUNK_MS // 1000  # 2400
MAX_BATCH_SECONDS = 15
MAX_BATCH_SAMPLES = SAMPLE_RATE * MAX_BATCH_SECONDS  # 240000


class AudioBuffer:
    """Thread-safe FIFO buffer for audio samples.

    Callback appends chunks (2400 samples each).
    Sender extracts batches (2400 to 240000 samples, krate 2400).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._samples: bytearray = bytearray()
        self._total_appended = 0
        self._total_extracted = 0

    def append(self, chunk_bytes: bytes) -> None:
        """Append audio chunk from callback. Thread-safe.

        Args:
            chunk_bytes: PCM int16 samples (expected 2400 samples = 4800 bytes)
        """
        with self._lock:
            self._samples.extend(chunk_bytes)
            self._total_appended += len(chunk_bytes)

    def extract_batch(self) -> Optional[bytes]:
        """Extract max possible batch. Thread-safe.

        Returns:
            bytes of PCM samples (2400 to 240000 samples), or None if no full block.
            Samples stay in buffer until caller confirms successful send via commit().
        """
        with self._lock:
            available_samples = len(self._samples) // 2

            if available_samples < BLOCK_SIZE_SAMPLES:
                return None

            blocks_available = available_samples // BLOCK_SIZE_SAMPLES
            blocks_to_send = min(blocks_available, MAX_BATCH_SAMPLES // BLOCK_SIZE_SAMPLES)
            samples_to_send = blocks_to_send * BLOCK_SIZE_SAMPLES
            bytes_to_send = samples_to_send * 2

            batch = bytes(self._samples[:bytes_to_send])
            return batch

    def commit(self, bytes_count: int) -> None:
        """Remove successfully sent samples from buffer. Thread-safe.

        Must be called AFTER gRPC send succeeds.
        If send fails, do NOT call commit() - samples remain available for retry.

        Args:
            bytes_count: Number of bytes successfully sent (must match extract_batch result)
        """
        with self._lock:
            if bytes_count > 0 and bytes_count <= len(self._samples):
                self._samples = self._samples[bytes_count:]
                self._total_extracted += bytes_count

    def get_backlog_seconds(self) -> float:
        """Get current backlog duration in seconds. Thread-safe."""
        with self._lock:
            samples = len(self._samples) // 2
            return samples / SAMPLE_RATE

    def get_stats(self) -> Tuple[int, int, int]:
        """Get (total_appended_bytes, total_extracted_bytes, current_bytes). Thread-safe."""
        with self._lock:
            return (
                self._total_appended,
                self._total_extracted,
                len(self._samples),
            )
