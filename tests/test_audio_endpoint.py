from __future__ import annotations

import io
import sys
import types
import wave
from datetime import datetime
from pathlib import Path

import pytest


def _install_stubs():
    """Install stubs for fastapi, boto3, and other dependencies."""
    if "fastapi" in sys.modules and hasattr(sys.modules["fastapi"], "FastAPI"):
        return

    # FastAPI stubs
    fastapi_stub = types.ModuleType("fastapi")

    class _FastAPI:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, *args, **kwargs):
            def decorator(fn):
                return fn
            return decorator

    class _HTTPException(Exception):
        def __init__(self, status_code=None, detail=None):
            self.status_code = status_code
            self.detail = detail

    fastapi_stub.FastAPI = _FastAPI
    fastapi_stub.HTTPException = _HTTPException
    fastapi_stub.Query = lambda default=None, description=None: default

    class _FileResponse:
        def __init__(self, path=None, filename=None, media_type=None):
            self.path = str(path)
            self.filename = filename
            self.media_type = media_type

    class _HTMLResponse(str):
        pass

    class _Response:
        def __init__(self, content=None, media_type=None, headers=None):
            self.content = content
            self.media_type = media_type
            self.headers = headers or {}

    responses_stub = types.ModuleType("fastapi.responses")
    responses_stub.FileResponse = _FileResponse
    responses_stub.HTMLResponse = _HTMLResponse
    responses_stub.Response = _Response
    fastapi_stub.responses = responses_stub

    sys.modules["fastapi"] = fastapi_stub
    sys.modules["fastapi.responses"] = responses_stub

    # Boto3 stubs
    boto3_stub = types.ModuleType("boto3")

    class _MockS3Client:
        def __init__(self, *args, **kwargs):
            self.objects = []

        def get_paginator(self, operation_name):
            class Paginator:
                def paginate(self, Bucket=None, Prefix=None):
                    return [{"Contents": self._client.objects}]
            paginator = Paginator()
            paginator._client = self
            return paginator

        def get_object(self, Bucket=None, Key=None):
            for obj in self.objects:
                if obj["Key"] == Key:
                    return {"Body": _MockBody(obj["Body"])}
            raise Exception(f"Object not found: {Key}")

    class _MockBody:
        def __init__(self, data):
            self.data = data

        def read(self):
            return self.data

    boto3_stub.client = lambda *args, **kwargs: _MockS3Client()
    sys.modules["boto3"] = boto3_stub


def _load_api():
    if "stats_service.api" in sys.modules:
        return sys.modules["stats_service.api"]
    _install_stubs()
    import importlib
    return importlib.import_module("stats_service.api")


@pytest.fixture
def api_module():
    return _load_api()


@pytest.fixture
def mock_s3_client():
    """Create a mock S3 client with test data."""
    _install_stubs()
    import boto3
    client = boto3.client("s3")

    # Create mock WAV data for chunks
    def create_wav_chunk(pcm_data: bytes) -> bytes:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(pcm_data)
        return buffer.getvalue()

    # Create test sessions
    session1_chunks = [
        {"Key": "audio/20260903-172000-г_Минводы/000001.wav", "Body": create_wav_chunk(b"\x00\x01" * 16000)},  # 1 sec
        {"Key": "audio/20260903-172000-г_Минводы/000002.wav", "Body": create_wav_chunk(b"\x00\x02" * 16000)},  # 1 sec
    ]

    session2_chunks = [
        {"Key": "audio/20260903-173000-г_Минводы/000001.wav", "Body": create_wav_chunk(b"\x00\x03" * 16000)},  # 1 sec
    ]

    # Non-matching session (different store)
    other_store_chunks = [
        {"Key": "audio/20260903-172500-г_Пятигорск/000001.wav", "Body": create_wav_chunk(b"\x00\x04" * 16000)},
    ]

    client.objects = session1_chunks + session2_chunks + other_store_chunks

    # Override get_paginator to filter by prefix
    def mock_get_paginator(operation_name):
        class Paginator:
            def paginate(self, Bucket=None, Prefix=None):
                filtered = [obj for obj in self._client.objects if obj["Key"].startswith(Prefix)]
                return [{"Contents": filtered}]
        paginator = Paginator()
        paginator._client = client
        return paginator

    client.get_paginator = mock_get_paginator
    return client


def test_list_sessions_for_store_returns_matching_sessions(api_module, mock_s3_client):
    sessions = api_module.list_sessions_for_store(mock_s3_client, "20260903", "г_Минводы")
    assert len(sessions) == 2
    assert "20260903-172000-г_Минводы" in sessions
    assert "20260903-173000-г_Минводы" in sessions


def test_list_sessions_for_store_filters_by_store(api_module, mock_s3_client):
    sessions = api_module.list_sessions_for_store(mock_s3_client, "20260903", "г_Пятигорск")
    assert len(sessions) == 1
    assert "20260903-172500-г_Пятигорск" in sessions


def test_get_session_time_minutes_correctly_parses_session_id(api_module):
    session_id = "20260903-172000-г_Минводы"
    minutes = api_module.get_session_time_minutes(session_id)
    assert minutes == 17 * 60 + 20  # 1040 minutes


def test_get_session_time_minutes_handles_invalid_session_id(api_module):
    session_id = "invalid-session-id"
    minutes = api_module.get_session_time_minutes(session_id)
    assert minutes is None


def test_download_session_chunks_returns_concatenated_pcm(api_module, mock_s3_client):
    audio_data = api_module.download_session_chunks(mock_s3_client, "20260903-172000-г_Минводы")
    # Each chunk is 1 sec = 16000 samples * 2 bytes = 32000 bytes
    # Two chunks for this session = 64000 bytes
    assert len(audio_data) == 64000


def test_merge_audio_sessions_creates_valid_wav(api_module):
    # Create test PCM data
    pcm1 = b"\x00\x01" * 16000  # 1 sec
    pcm2 = b"\x00\x02" * 16000  # 1 sec

    buffer = api_module.merge_audio_sessions([pcm1, pcm2])

    # Verify the result is a valid WAV
    with wave.open(buffer, "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000
        assert wf.getnframes() == 32000  # 2 seconds


def test_merge_audio_sessions_no_silence_padding(api_module):
    """Verify that no silence is added between sessions."""
    # Create distinct PCM patterns
    pcm1 = b"\x00\x01" * 8000  # 0.5 sec of pattern 1
    pcm2 = b"\x00\x02" * 8000  # 0.5 sec of pattern 2

    buffer = api_module.merge_audio_sessions([pcm1, pcm2])

    # Read the merged audio
    buffer.seek(0)
    with wave.open(buffer, "rb") as wf:
        frames = wf.readframes(wf.getnframes())

    # Total should be exactly 32000 bytes (0.5 + 0.5 sec * 2 bytes per sample * 16000 samples/sec)
    # No extra silence between sessions
    assert len(frames) == 32000

    # Verify the patterns are contiguous (no zeros between them)
    # First 16000 bytes should be pattern 1
    assert frames[:16] == b"\x00\x01" * 8
    # Next 16000 bytes should be pattern 2 (no zeros in between)
    assert frames[32000-16:32000] == b"\x00\x02" * 8


def test_download_audio_endpoint_validation_empty_store(api_module):
    """Test that empty store name raises HTTPException."""
    with pytest.raises(api_module.HTTPException) as exc_info:
        api_module.download_audio(
            store="",
            start_time=datetime(2026, 9, 3, 17, 20),
            end_time=datetime(2026, 9, 3, 17, 45),
        )
    assert exc_info.value.status_code == 400
    assert "required" in exc_info.value.detail.lower()


def test_download_audio_endpoint_validation_start_after_end(api_module):
    """Test that start_time >= end_time raises HTTPException."""
    with pytest.raises(api_module.HTTPException) as exc_info:
        api_module.download_audio(
            store="Test Store",
            start_time=datetime(2026, 9, 3, 17, 45),
            end_time=datetime(2026, 9, 3, 17, 20),
        )
    assert exc_info.value.status_code == 400
    assert "before" in exc_info.value.detail.lower()


def test_download_audio_endpoint_no_sessions_found(api_module, mock_s3_client):
    """Test that no sessions found raises HTTPException."""
    with pytest.raises(api_module.HTTPException) as exc_info:
        api_module.download_audio(
            store="NonExistentStore",
            start_time=datetime(2026, 9, 3, 17, 20),
            end_time=datetime(2026, 9, 3, 17, 45),
        )
    assert exc_info.value.status_code == 404
    assert "found" in exc_info.value.detail.lower()


def test_download_audio_endpoint_includes_sessions_starting_before_range(api_module, mock_s3_client, monkeypatch):
    """Test that sessions starting before the range but ending within it are included."""
    # Patch get_s3_client to return our mock
    monkeypatch.setattr(api_module, "get_s3_client", lambda: mock_s3_client)

    # Request range 17:25-17:35
    # Session at 17:20 should be included (starts before but overlaps)
    response = api_module.download_audio(
        store="г_Минводы",
        start_time=datetime(2026, 9, 3, 17, 25),
        end_time=datetime(2026, 9, 3, 17, 35),
    )
    # Should return a Response with audio content
    assert response is not None
    assert response.media_type == "audio/wav"
    assert len(response.content) > 0


def test_audio_page_returns_html(api_module):
    """Test that the audio page endpoint returns HTML."""
    result = api_module.audio_page()
    assert isinstance(result, str)
    assert "<html" in result.lower()
    assert "Аудио по отчётам" in result
    assert 'id="date-select"' in result
    assert 'id="store-select"' in result
    # client-select was removed from UI
    assert 'id="client-select"' not in result


def test_index_page_has_audio_link(api_module):
    """Test that the index page has a link to the audio page."""
    result = api_module.index_page()
    assert isinstance(result, str)
    assert '<a href="/audio"' in result
    assert "Скачать аудио по магазину" in result
