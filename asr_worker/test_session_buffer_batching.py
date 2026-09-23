"""Test SessionBuffer adaptive batching for ASR worker.

Tests extract_batch() and commit() methods with adaptive batching:
  - 160ms, 320ms, 480ms, 1.6s, 10s, 14.88s, 15s, 20s, 30s
  - Error handling (no commit on failure)
  - No loss (all samples preserved)
  - Order preservation
"""

import asyncio
import struct
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from asr_worker.session_buffer import (
    SessionBuffer,
    ASR_BLOCK_SIZE_BYTES,
    ASR_MAX_BATCH_CHUNKS,
    ASR_MAX_BATCH_BYTES,
    ASR_CHUNK_MS,
)


def create_test_chunk(chunk_num: int) -> bytes:
    """Create a 160ms chunk with unique sequential samples."""
    samples_per_chunk = 2560  # 160ms @ 16kHz
    samples = []
    for i in range(samples_per_chunk):
        val = (chunk_num * samples_per_chunk + i) & 0x7FFF
        samples.append(val)
    return struct.pack(f"{samples_per_chunk}h", *samples)


async def test_160ms_backlog_sends_160ms():
    """160ms backlog → 1 chunk (160ms)"""
    buffer = SessionBuffer()
    await buffer.add("session1", 1, create_test_chunk(0))

    batch = await buffer.extract_batch("session1")
    assert batch is not None, "Should have batch"
    assert len(batch) == ASR_BLOCK_SIZE_BYTES, f"Expected {ASR_BLOCK_SIZE_BYTES} bytes, got {len(batch)}"

    await buffer.commit("session1", len(batch))


async def test_320ms_backlog_sends_320ms():
    """320ms backlog → 2 chunks (320ms)"""
    buffer = SessionBuffer()
    await buffer.add("session1", 1, create_test_chunk(0))
    await buffer.add("session1", 2, create_test_chunk(1))

    batch = await buffer.extract_batch("session1")
    assert batch is not None
    assert len(batch) == 2 * ASR_BLOCK_SIZE_BYTES

    await buffer.commit("session1", len(batch))


async def test_480ms_backlog_sends_480ms():
    """480ms backlog → 3 chunks (480ms)"""
    buffer = SessionBuffer()
    for i in range(3):
        await buffer.add("session1", i + 1, create_test_chunk(i))

    batch = await buffer.extract_batch("session1")
    assert batch is not None
    assert len(batch) == 3 * ASR_BLOCK_SIZE_BYTES

    await buffer.commit("session1", len(batch))


async def test_1_6s_backlog_sends_1_6s():
    """1.6s backlog → 10 chunks (1.6s)"""
    buffer = SessionBuffer()
    for i in range(10):
        await buffer.add("session1", i + 1, create_test_chunk(i))

    batch = await buffer.extract_batch("session1")
    assert batch is not None
    assert len(batch) == 10 * ASR_BLOCK_SIZE_BYTES

    await buffer.commit("session1", len(batch))


async def test_10s_backlog_sends_10s():
    """10s backlog → 62 chunks (9.92s)"""
    buffer = SessionBuffer()
    chunks_in_10s = 10 * 1000 // ASR_CHUNK_MS  # 62 chunks
    for i in range(chunks_in_10s):
        await buffer.add("session1", i + 1, create_test_chunk(i))

    batch = await buffer.extract_batch("session1")
    assert batch is not None
    assert len(batch) == chunks_in_10s * ASR_BLOCK_SIZE_BYTES

    await buffer.commit("session1", len(batch))


async def test_14_88s_backlog_sends_14_88s():
    """14.88s backlog → 93 chunks (14.88s = MAX)"""
    buffer = SessionBuffer()
    for i in range(ASR_MAX_BATCH_CHUNKS):
        await buffer.add("session1", i + 1, create_test_chunk(i))

    batch = await buffer.extract_batch("session1")
    assert batch is not None
    assert len(batch) == ASR_MAX_BATCH_CHUNKS * ASR_BLOCK_SIZE_BYTES
    assert len(batch) == ASR_MAX_BATCH_BYTES

    await buffer.commit("session1", len(batch))


async def test_20s_backlog_sends_14_88s_plus_remainder():
    """20s backlog → 93 chunks (14.88s) + remainder"""
    buffer = SessionBuffer()
    chunks_in_20s = 20 * 1000 // ASR_CHUNK_MS  # 125 chunks
    for i in range(chunks_in_20s):
        await buffer.add("session1", i + 1, create_test_chunk(i))

    # First batch: max 93 chunks
    batch1 = await buffer.extract_batch("session1")
    assert batch1 is not None
    assert len(batch1) == ASR_MAX_BATCH_BYTES

    await buffer.commit("session1", len(batch1))

    # Second batch: remaining 32 chunks
    batch2 = await buffer.extract_batch("session1")
    assert batch2 is not None
    assert len(batch2) == (chunks_in_20s - ASR_MAX_BATCH_CHUNKS) * ASR_BLOCK_SIZE_BYTES

    await buffer.commit("session1", len(batch2))


async def test_30s_backlog_sends_multiple_batches():
    """30s backlog → 14.88s + 14.88s + 240ms (3 batches)"""
    buffer = SessionBuffer()
    chunks_in_30s = 30 * 1000 // ASR_CHUNK_MS  # 187 chunks
    for i in range(chunks_in_30s):
        await buffer.add("session1", i + 1, create_test_chunk(i))

    # Batch 1: 93 chunks
    batch1 = await buffer.extract_batch("session1")
    assert len(batch1) == ASR_MAX_BATCH_BYTES
    await buffer.commit("session1", len(batch1))

    # Batch 2: 93 chunks
    batch2 = await buffer.extract_batch("session1")
    assert len(batch2) == ASR_MAX_BATCH_BYTES
    await buffer.commit("session1", len(batch2))

    # Batch 3: 1 chunk (160ms)
    batch3 = await buffer.extract_batch("session1")
    assert batch3 is not None
    assert len(batch3) == ASR_BLOCK_SIZE_BYTES

    await buffer.commit("session1", len(batch3))


async def test_retry_samples_remain_in_buffer():
    """Error on send → samples remain in buffer for retry"""
    buffer = SessionBuffer()
    await buffer.add("session1", 1, create_test_chunk(0))
    await buffer.add("session1", 2, create_test_chunk(1))

    # Extract batch
    batch1 = await buffer.extract_batch("session1")
    assert len(batch1) == 2 * ASR_BLOCK_SIZE_BYTES

    # Simulate error: do NOT commit
    # Extract again: same data should be available
    batch2 = await buffer.extract_batch("session1")
    assert len(batch2) == 2 * ASR_BLOCK_SIZE_BYTES
    assert batch1 == batch2

    # Now commit
    await buffer.commit("session1", len(batch1))


async def test_no_loss_all_samples_preserved():
    """All input samples must be output (no loss)"""
    buffer = SessionBuffer()
    total_chunks = 150
    total_input_bytes = total_chunks * ASR_BLOCK_SIZE_BYTES

    for i in range(total_chunks):
        await buffer.add("session1", i + 1, create_test_chunk(i))

    total_output_bytes = 0
    while True:
        batch = await buffer.extract_batch("session1")
        if batch is None:
            break
        total_output_bytes += len(batch)
        await buffer.commit("session1", len(batch))

    assert total_output_bytes == total_input_bytes, f"Lost {total_input_bytes - total_output_bytes} bytes"


async def test_order_preservation():
    """Output order must match input order"""
    buffer = SessionBuffer()
    num_chunks = 100

    for i in range(num_chunks):
        await buffer.add("session1", i + 1, create_test_chunk(i))

    all_output = bytearray()
    while True:
        batch = await buffer.extract_batch("session1")
        if batch is None:
            break
        all_output.extend(batch)
        await buffer.commit("session1", len(batch))

    # Reconstruct expected
    expected = bytearray()
    for i in range(num_chunks):
        expected.extend(create_test_chunk(i))

    assert bytes(all_output) == bytes(expected), "Order mismatch"


async def test_extract_batch_does_not_remove():
    """extract_batch() must not remove data until commit()"""
    buffer = SessionBuffer()
    await buffer.add("session1", 1, create_test_chunk(0))

    # Extract multiple times without commit
    batch1 = await buffer.extract_batch("session1")
    batch2 = await buffer.extract_batch("session1")
    batch3 = await buffer.extract_batch("session1")

    assert batch1 == batch2 == batch3, "extract_batch must not modify buffer"

    # After commit, no data
    await buffer.commit("session1", len(batch1))
    batch4 = await buffer.extract_batch("session1")
    assert batch4 is None


async def main():
    print("Running SessionBuffer adaptive batching tests...")
    
    await test_160ms_backlog_sends_160ms()
    print("✓ test_160ms_backlog_sends_160ms")
    
    await test_320ms_backlog_sends_320ms()
    print("✓ test_320ms_backlog_sends_320ms")
    
    await test_480ms_backlog_sends_480ms()
    print("✓ test_480ms_backlog_sends_480ms")
    
    await test_1_6s_backlog_sends_1_6s()
    print("✓ test_1_6s_backlog_sends_1_6s")
    
    await test_10s_backlog_sends_10s()
    print("✓ test_10s_backlog_sends_10s")
    
    await test_14_88s_backlog_sends_14_88s()
    print("✓ test_14_88s_backlog_sends_14_88s")
    
    await test_20s_backlog_sends_14_88s_plus_remainder()
    print("✓ test_20s_backlog_sends_14_88s_plus_remainder")
    
    await test_30s_backlog_sends_multiple_batches()
    print("✓ test_30s_backlog_sends_multiple_batches")
    
    await test_retry_samples_remain_in_buffer()
    print("✓ test_retry_samples_remain_in_buffer")
    
    await test_no_loss_all_samples_preserved()
    print("✓ test_no_loss_all_samples_preserved")
    
    await test_order_preservation()
    print("✓ test_order_preservation")
    
    await test_extract_batch_does_not_remove()
    print("✓ test_extract_batch_does_not_remove")
    
    print("\nAll tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
