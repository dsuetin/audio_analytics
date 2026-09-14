#!/usr/bin/env python3
"""
Test variable-length payload through gRPC to VAD server.

Tests various payload sizes end-to-end:
1. 150ms (baseline)
2. 450ms
3. 1500ms
4. 3000ms
5. 6000ms
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


def test_payload_size(ms, sequence_id, description=""):
    """Test a specific payload size through gRPC."""
    samples = int(ms * 16000 / 1000)
    print(f"\n{'='*60}")
    print(f"Test: {ms}ms ({samples} samples) {description}")
    print(f"{'='*60}")
    
    # Generate test audio (speech-like)
    audio = generate_speech_like(samples)
    audio_np = audio.reshape(1, -1).astype(np.int16)
    
    print(f"Audio shape: {audio_np.shape}")
    
    # Prepare inputs
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
        # Connect to Triton
        client = grpcclient.InferenceServerClient(url=URL)
        
        # Send request
        result = client.infer(
            MODEL,
            infer_inputs,
            outputs=outputs,
            sequence_id=sequence_id,
            sequence_start=True,
            sequence_end=True,
        )
        
        # Get response
        output = result.as_numpy("Response").item()
        print(f"Response received successfully")
        
        print(f"✓ PASS: {ms}ms ({samples} samples)")
        return True
        
    except Exception as e:
        print(f"✗ FAIL: {ms}ms ({samples} samples)")
        print(f"  Error: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Run all tests."""
    print("\n" + "="*60)
    print("VAD Variable-Length Payload Tests (gRPC)")
    print("="*60)
    
    results = []
    
    # Test various payload sizes
    print("\n--- Payload Size Tests ---")
    for i, ms in enumerate([150, 300, 450, 600, 750, 1500, 3000, 6000]):
        sequence_id = 1000 + i
        results.append(test_payload_size(ms, sequence_id))
    
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
