from __future__ import annotations

import sys
import types
from datetime import date
from pathlib import Path

import pytest


def _install_fastapi_stub():
    if "fastapi" in sys.modules and hasattr(sys.modules["fastapi"], "FastAPI"):
        return
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

    class _FileResponse:
        def __init__(self, path=None, filename=None, media_type=None):
            self.path = str(path)
            self.filename = filename
            self.media_type = media_type

    class _HTMLResponse(str):
        pass

    responses_stub = types.ModuleType("fastapi.responses")
    responses_stub.FileResponse = _FileResponse
    responses_stub.HTMLResponse = _HTMLResponse
    fastapi_stub.responses = responses_stub

    sys.modules["fastapi"] = fastapi_stub
    sys.modules["fastapi.responses"] = responses_stub
    if "uvicorn" not in sys.modules:
        sys.modules["uvicorn"] = types.ModuleType("uvicorn")


def _load_api():
    if "stats_service.api" in sys.modules:
        return sys.modules["stats_service.api"]
    _install_fastapi_stub()
    import importlib

    return importlib.import_module("stats_service.api")


@pytest.fixture
def api_module(monkeypatch, tmp_path):
    api = _load_api()
    monkeypatch.setattr(api, "REPORT_DIR", tmp_path)
    return api


def test_endpoint_returns_existing_final_report(api_module):
    final = api_module.REPORT_DIR / "final_transcript_report_2026-08-26.xlsx"
    final.write_bytes(b"excel-data")

    response = api_module.generate_report(date(2026, 8, 26))

    assert response.path == str(final)


def test_endpoint_does_not_generate_when_only_raw_exists(api_module):
    raw = api_module.REPORT_DIR / "transcript_report_2026-08-26.xlsx"
    raw.write_bytes(b"raw")

    with pytest.raises(api_module.HTTPException) as exc_info:
        api_module.generate_report(date(2026, 8, 26))

    assert exc_info.value.status_code == 404
    assert not (api_module.REPORT_DIR / "final_transcript_report_2026-08-26.xlsx").exists()


def test_endpoint_reports_missing_report_when_nothing_exists(api_module):
    with pytest.raises(api_module.HTTPException) as exc_info:
        api_module.generate_report(date(2026, 12, 31))

    assert exc_info.value.status_code == 404
    assert "2026-12-31" in exc_info.value.detail
    assert "не существует" in exc_info.value.detail
