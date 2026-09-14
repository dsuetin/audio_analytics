#!/usr/bin/env python3
"""
Test VAD edge cases through gRPC.

Tests:
1. First request < 2400 samples
2. First request = 0 samples
3. Remainder handling (N % 2400 != 0)
4. Sequence semantics (start/middle/end)
"""

import numpy as np
import tritonclient.grpc as grpcclient

MODEL = "online_vad"
URL = "server_triton-speech-segmentation:8001"


def generate_silence(samples):
    """Generate silence (zeros)."""
    return np.zeros(samples, dtype=np.int16)


def generate_speech_like(samples):
    """Generate speech-like signal (sine wave at 200Hz)."""
    t = np.linspace(0, samples / 16000, samples)
    audio = (0.3 * 32767 * np.sin(2 * np.pi * 200 * t)).astype(np.int16)
    return audio


def send_request(audio_np, sequence_id, sequence_start=False, sequence_end=False, description=""):
    """Send a single request to VAD."""
    print(f"\n  {description}")
    print(f"  Audio shape: {audio_np.shape}, sequence_start={sequence_start}, sequence_end={sequence_end}")
    
    threshold = np.array([[0.2]], dtype=np.float16)
    min_silence = np.array([[500]], dtype=np.int16)
    mode = np.array([[b"ONLY_SPEECH"]])
    
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
    
    try:
        client = grpcclient.InferenceServerClient(url=URL)
        result = client.infer(
            MODEL,
            infer_inputs,
            outputs=outputs,
            sequence_id=sequence_id,
            sequence_start=sequence_start,
            sequence_end=sequence_end,
        )
        output = result.as_numpy("Response").item()
        print(f"  ✓ Response received")
        return True
    except Exception as e:
        print(f"  ✗ Error: {e}")
        return False


def test_small_first_request():
    """Test first request < 2400 samples, then normal request."""
    print(f"\n{'='*60}")
    print(f"Test: First request < 2400 samples")
    print(f"{'='*60}")
    
    sequence_id = 5001
    
    # First request: 1200 samples (< 2400)
    audio1 = generate_speech_like(1200).reshape(1, -1)
    result1 = send_request(audio1, sequence_id, sequence_start=True, sequence_end=False,
                          description="Request 1: 1200 samples (sequence_start)")
    
    # Second request: 4800 samples
    audio2 = generate_speech_like(4800).reshape(1, -1)
    result2 = send_request(audio2, sequence_id, sequence_start=False, sequence_end=True,
                          description="Request 2: 4800 samples (sequence_end)")
    
    return result1 and result2


def test_zero_samples():
    """Test first request = 0 samples."""
    print(f"\n{'='*60}")
    print(f"Test: First request = 0 samples")
    print(f"{'='*60}")
    
    sequence_id = 5002
    
    # Zero samples
    audio = np.zeros((1, 0), dtype=np.int16)
    result = send_request(audio, sequence_id, sequence_start=True, sequence_end=True,
                         description="Request: 0 samples (sequence_start + sequence_end)")
    
    return result


def test_remainder():
    """Test payload not divisible by 2400."""
    print(f"\n{'='*60}")
    print(f"Test: Remainder handling (2500 samples = 1 window + 100 remainder)")
    print(f"{'='*60}")
    
    sequence_id = 5003
    
    # 2500 samples (not divisible by 2400)
    audio = generate_speech_like(2500).reshape(1, -1)
    result = send_request(audio, sequence_id, sequence_start=True, sequence_end=True,
                         description="Request: 2500 samples (2400 + 100 remainder)")
    
    return result


def test_sequence_semantics():
    """Test sequence start/middle/end semantics."""
    print(f"\n{'='*60}")
    print(f"Test: Sequence semantics (start/middle/end)")
    print(f"{'='*60}")
    
    sequence_id = 5004
    
    # Start: 2400 samples
    audio1 = generate_speech_like(2400).reshape(1, -1)
    result1 = send_request(audio1, sequence_id, sequence_start=True, sequence_end=False,
                          description="Request 1: 2400 samples (sequence_start)")
    
    # Middle: 4800 samples
    audio2 = generate_speech_like(4800).reshape(1, -1)
    result2 = send_request(audio2, sequence_id, sequence_start=False, sequence_end=False,
                          description="Request 2: 4800 samples (middle)")
    
    # End: 3600 samples
    audio3 = generate_speech_like(3600).reshape(1, -1)
    result3 = send_request(audio3, sequence_id, sequence_start=False, sequence_end=True,
                          description="Request 3: 3600 samples (sequence_end)")
    
    return result1 and result2 and result3


def test_mixed_sizes():
    """Test mixed payload sizes in sequence."""
    print(f"\n{'='*60}")
    print(f"Test: Mixed payload sizes in sequence")
    print(f"{'='*60}")
    
    sequence_id = 5005
    
    # Small: 1200 samples
    audio1 = generate_speech_like(1200).reshape(1, -1)
    result1 = send_request(audio1, sequence_id, sequence_start=True, sequence_end=False,
                          description="Request 1: 1200 samples (small)")
    
    # Large: 9600 samples
    audio2 = generate_speech_like(9600).reshape(1, -1)
    result2 = send_request(audio2, sequence_id, sequence_start=False, sequence_end=False,
                          description="Request 2: 9600 samples (large)")
    
    # Medium with remainder: 2500 samples
    audio3 = generate_speech_like(2500).reshape(1, -1)
    result3 = send_request(audio3, sequence_id, sequence_start=False, sequence_end=True,
                          description="Request 3: 2500 samples (medium + remainder)")
    
    return result1 and result2 and result3


def main():
    """Run all tests."""
    print("\n" + "="*60)
    print("VAD Edge Case Tests (gRPC)")
    print("="*60)
    
    results = []
    
    print("\n--- Edge Case Tests ---")
    results.append(test_small_first_request())
    results.append(test_zero_samples())
    results.append(test_remainder())
    results.append(test_sequence_semantics())
    results.append(test_mixed_sizes())
    
    # Summary
    print("\n" + "="*60)
    print("Summary")
    print("="*60)
    passed = sum(results)
    total = len(results)
    print(f"Passed: {passed}/{total}")
    
    if passed == total:
        print("\n✓ All tests passed!")
        return 0
    else:
        print(f"\n✗ {total - passed} test(s) failed")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
