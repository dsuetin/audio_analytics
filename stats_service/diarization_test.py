"""Страница тестирования PF-модели `diarization_online_pf` (/diarization-test).

Ручной тест PARTIAL FINALIZATION + стабильных speaker ID через Triton gRPC.
Изолировано: без Kafka, PostgreSQL и S3; результаты — в REPORT_DIR/diarization_test
и в памяти процесса. Production-поток (worker, модели) не используется.
"""
import asyncio
import io
import json
import logging
import os
import queue
import time
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response

from stats_service.diarization_protocol import output_pb2

logger = logging.getLogger("stats_service.diarization_test")

router = APIRouter(prefix="/diarization-test", tags=["diarization-test"])

TRITON_URL = os.getenv("TRITON_PF_URL", "server_triton-speech-segmentation:8001")
MODEL_NAME = os.getenv("TRITON_PF_MODEL", "diarization_online_pf")
SAMPLE_RATE = 16000
DEFAULT_CHUNK_MS = 3000
PARTIAL_MODES = ("none", "each", "5", "10", "20", "manual", "before_full")

STORAGE = Path(os.getenv("REPORT_OUTPUT_DIR", "/reports")) / "diarization_test"

# tritonclient — ленивый импорт: production-образ stats_service может не
# содержать gRPC-зависимости (guarded include в api.py); страница тогда
# показывает понятную ошибку вместо падения всего сервиса.
try:
    from tritonclient.grpc import (
        InferenceServerClient,
        InferInput,
        InferRequestedOutput,
    )
    TRITON_IMPORT_ERROR: Optional[str] = None
except Exception as _e:  # pragma: no cover
    InferenceServerClient = InferInput = InferRequestedOutput = None
    TRITON_IMPORT_ERROR = str(_e)


def _require_triton():
    if TRITON_IMPORT_ERROR is not None:
        raise HTTPException(
            503,
            "gRPC-зависимости stats_service не установлены "
            f"({TRITON_IMPORT_ERROR}). Установите grpcio, tritonclient, "
            "protobuf, numpy (stats_service/requirements.txt) и перезапустите "
            "контейнер.",
        )


@dataclass
class TestSession:
    session_id: str
    filename: str
    pcm: np.ndarray                      # mono int16, 16 kHz
    file_info: dict
    chunk_ms: int = DEFAULT_CHUNK_MS
    partial_mode: str = "none"
    sequence_id: int = 0
    chunk_index: int = 0                 # отправленных модельных ячеек (аудио + сигналы)
    cells_sent: int = 0                  # только аудио-ячейки
    state: str = "uploaded"              # uploaded|running|paused|finalized|cancelled|error
    triton: object = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    partial_snapshots: list = field(default_factory=list)
    final_segments: list = field(default_factory=list)
    debug: list = field(default_factory=list)
    error: Optional[str] = None
    created: float = field(default_factory=time.time)

    @property
    def audio_cells_total(self) -> int:
        spc = self.chunk_ms * SAMPLE_RATE // 1000
        return max(1, (len(self.pcm) + spc - 1) // spc)


class _PfTriton:
    """Paced-клиент Triton для одной тестовой сессии.

    Точная копия проверенного worker-паттерна: async_stream_infer + очередь
    callback'ов, одно запрос в полёте, enable_empty_final_response=True.
    """

    def __init__(self):
        self.q: "queue.Queue" = queue.Queue()
        self.client = InferenceServerClient(url=TRITON_URL)

    def _cb(self, result=None, error=None):
        if error:
            self.q.put(("error", error))
        else:
            self.q.put(("result", result))

    def start(self):
        self.client.start_stream(callback=self._cb)

    def model_ready(self) -> bool:
        try:
            return bool(self.client.is_model_ready(MODEL_NAME))
        except Exception as e:
            raise RuntimeError(f"Triton {TRITON_URL} недоступен: {e}")

    def send(self, seq_id: int, audio: np.ndarray, marks_bytes: bytes,
             chunk_id: int, sequence_start: bool, sequence_end: bool,
             partial_finalization: str, request_id: str,
             timeout: float = 120.0):
        """Один запрос + paced-ожидание ответа. Возврат: DiarisationResponse или None."""
        input_np = audio.astype(np.int16).reshape(1, -1)
        vad_np = np.array([[marks_bytes]], dtype=object)
        cid_np = np.array([[chunk_id]], dtype=np.uint16)
        inputs = [
            InferInput("input", input_np.shape, "INT16"),
            InferInput("vad_marks", vad_np.shape, "BYTES"),
            InferInput("chunk_id", cid_np.shape, "UINT16"),
        ]
        inputs[0].set_data_from_numpy(input_np)
        inputs[1].set_data_from_numpy(vad_np)
        inputs[2].set_data_from_numpy(cid_np)
        outputs = [InferRequestedOutput("DiarisationResponse")]
        params = {
            "sample_rate": "16000",
            "window_fs": "1.52",
            "period_fs": "0.7",
            "frame_shift": "10",
            "max_speaker": "20",
            "num_speaker": "0",
            "use_audiogram_vad": "True",
            "partial_finalization": partial_finalization,
        }
        self.client.async_stream_infer(
            MODEL_NAME, inputs, request_id=request_id, outputs=outputs,
            parameters=params, sequence_id=seq_id,
            sequence_start=sequence_start, sequence_end=sequence_end,
            enable_empty_final_response=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg_type, msg = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if msg_type == "error":
                raise RuntimeError(f"Triton inference error: {msg}")
            if msg is None:
                continue
            try:
                out = msg.as_numpy("DiarisationResponse")
            except Exception:
                return None  # пустой финальный ответ (обычный для аудио-ячейки)
            if out is None or out.size == 0:
                return None
            data = out.tolist()
            if isinstance(data, (list, tuple)):
                data = data[0]
            if not isinstance(data, (bytes, bytearray)):
                data = bytes(data)
            return output_pb2.DiarisationResponse.FromString(data)
        raise TimeoutError(f"таймаут {timeout}s ожидания ответа Triton")


def _build_marks(actual_ms: int) -> bytes:
    """vad_marks ячейки: BEGIN 0 -> END actual_ms (пустые marks для сигнала)."""
    seg = output_pb2.SpeakerSegment()
    if actual_ms > 0:
        b = output_pb2.VoiceActivityMark()
        b.mark_type = output_pb2.VoiceActivityMark.VA_MARK_BEGIN
        b.offset_ms = 0
        e = output_pb2.VoiceActivityMark()
        e.mark_type = output_pb2.VoiceActivityMark.VA_MARK_END
        e.offset_ms = actual_ms
        seg.va_marks.extend([b, e])
    return seg.SerializeToString()


def _parse_segments(resp) -> list:
    """DiarisationResponse -> [{start_ms, end_ms, speaker_id}] (модельное время)."""
    out = []
    if resp is None:
        return out
    for seg in resp.speaker_segment:
        if len(seg.va_marks) >= 2:
            out.append({
                "start_ms": int(seg.va_marks[0].offset_ms),
                "end_ms": int(seg.va_marks[1].offset_ms),
                "speaker_id": int(seg.speaker_id),
            })
    out.sort(key=lambda x: (x["start_ms"], x["end_ms"]))
    return out


def _decode_wav(data: bytes):
    """WAV bytes -> (pcm int16 mono 16 kHz, info). Исходный файл не изменяется."""
    try:
        wf = wave.open(io.BytesIO(data))
    except Exception as e:
        raise HTTPException(400, f"некорректный WAV: {e}")
    ch = wf.getnchannels()
    sw = wf.getsampwidth()
    sr = wf.getframerate()
    n = wf.getnframes()
    if ch <= 0 or sr <= 0 or n <= 0:
        raise HTTPException(400, "пустой/некорректный WAV-заголовок")
    raw = wf.readframes(n)
    info = {
        "channels": ch,
        "sample_width_bytes": sw,
        "sample_rate": sr,
        "frames": n,
        "duration_ms": int(n * 1000 / sr),
        "resampled": sr != SAMPLE_RATE,
        "mixed_to_mono": ch != 1,
    }
    if sw == 1:
        arr = np.frombuffer(raw, dtype=np.uint8).astype(np.int32) * 32767
    elif sw == 2:
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.int32)
    elif sw == 3:
        b = np.frombuffer(raw, dtype=np.uint8).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        arr = np.where(v >= 0x800000, v - 0x1000000, v)
    elif sw == 4:
        arr = np.frombuffer(raw, dtype=np.int32) >> 16
    else:
        raise HTTPException(
            400, f"неподдерживаемая разрядность {sw} байт "
                 "(float32 WAV не поддерживается модулем wave)")
    if ch > 1:
        arr = arr.reshape(-1, ch).mean(axis=1)
    if sr != SAMPLE_RATE:
        new_len = max(1, int(round(len(arr) / sr * SAMPLE_RATE)))
        old_x = np.arange(len(arr), dtype=np.float64)
        new_x = np.linspace(0.0, len(arr) - 1, new_len)
        arr = np.interp(new_x, old_x, arr)
    pcm = np.clip(arr, -32768, 32767).astype(np.int16)
    if len(pcm) < SAMPLE_RATE // 2:
        raise HTTPException(400, "аудио короче 50 мс — недостаточно для теста")
    return pcm, info


SESSIONS: dict = {}
_REGISTRY_LOCK = asyncio.Lock()


def _get(sid: str) -> TestSession:
    s = SESSIONS.get(sid)
    if s is None:
        raise HTTPException(404, f"сессия {sid} не найдена")
    return s


@router.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    _require_triton()
    data = await file.read()
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(413, "файл больше 200 МБ")
    pcm, info = _decode_wav(data)
    sid = uuid.uuid4().hex[:12]
    s = TestSession(
        session_id=sid,
        filename=file.filename or "upload.wav",
        pcm=pcm,
        file_info=info,
        sequence_id=int(uuid.uuid4().int >> 32) & 0x7FFFFFFFFFFFFFFF,
    )
    async with _REGISTRY_LOCK:
        SESSIONS[sid] = s
    try:
        STORAGE.mkdir(parents=True, exist_ok=True)
        (STORAGE / f"{sid}_orig.wav").write_bytes(data)
        (STORAGE / f"{sid}_pcm16k_mono.pcm").write_bytes(pcm.tobytes())
    except Exception as e:
        logger.warning("diarization-test: storage save failed: %s", e)
    return {
        "session_id": sid,
        "file": info,
        "sequence_id": s.sequence_id,
        "audio_cells_total": s.audio_cells_total,
        "chunk_ms_default": DEFAULT_CHUNK_MS,
        "partial_modes": list(PARTIAL_MODES),
        "model": MODEL_NAME,
        "triton_url": TRITON_URL,
    }


@router.post("/api/session/{sid}/config")
async def set_config(sid: str, body: dict):
    s = _get(sid)
    async with s.lock:
        if s.state not in ("uploaded",):
            raise HTTPException(409, f"настройки только до запуска (state={s.state})")
        chunk_ms = int(body.get("chunk_ms", DEFAULT_CHUNK_MS))
        mode = str(body.get("partial_mode", "none"))
        if not (250 <= chunk_ms <= 10000):
            raise HTTPException(400, "chunk_ms: от 250 до 10000")
        if mode not in PARTIAL_MODES:
            raise HTTPException(400, f"partial_mode: один из {list(PARTIAL_MODES)}")
        s.chunk_ms = chunk_ms
        s.partial_mode = mode
        s.state = "running"
    return {"state": s.state, "chunk_ms": chunk_ms,
            "partial_mode": mode, "audio_cells_total": s.audio_cells_total}


@router.get("/api/session/{sid}")
async def status(sid: str):
    s = _get(sid)
    latest = s.partial_snapshots[-1] if s.partial_snapshots else None
    segs = s.final_segments if s.state == "finalized" else (
        latest["segments"] if latest else [])
    speakers = sorted({x["speaker_id"] for x in segs})
    return {
        "state": s.state,
        "error": s.error,
        "cells_sent": s.cells_sent,
        "audio_cells_total": s.audio_cells_total,
        "chunk_index": s.chunk_index,
        "chunk_ms": s.chunk_ms,
        "partial_mode": s.partial_mode,
        "sent_audio_ms": min(
            s.cells_sent * s.chunk_ms, s.file_info["duration_ms"]),
        "duration_ms": s.file_info["duration_ms"],
        "partial_count": len(s.partial_snapshots),
        "partial_snapshots": s.partial_snapshots,
        "speakers": speakers,
        "latest_segments": segs,
        "final": s.state == "finalized",
    }


def _init_triton(s: TestSession):
    _require_triton()
    if s.triton is not None:
        return
    s.triton = _PfTriton()
    s.triton.start()
    if not s.triton.model_ready():
        s.triton = None
        raise HTTPException(
            503,
            f"модель {MODEL_NAME} не в состоянии READY на {TRITON_URL}. "
            "Загрузите модель: см. models/diarization_online_pf/MOUNT.md "
            "(вариант A: docker cp + gRPC load).",
        )


def _auto_partial_due(s: TestSession) -> bool:
    if s.partial_mode == "each":
        return True
    if s.partial_mode in ("5", "10", "20"):
        return s.cells_sent % int(s.partial_mode) == 0
    return False


def _do_debug(s: TestSession, tag: str, kind: str, chunk_id: int,
              ack_ms: int, segments: Optional[list] = None,
              error: Optional[str] = None):
    s.debug.append({
        "t": round(time.time(), 3),
        "tag": tag,
        "kind": kind,
        "chunk_id": chunk_id,
        "ack_ms": ack_ms,
        "segments": segments,
        "error": error,
    })


async def _send_cell(s: TestSession):
    """Одна аудио-ячейка (последняя — с доводом тишиной) + авто-PARTIAL."""
    spc = s.chunk_ms * SAMPLE_RATE // 1000
    offset = s.cells_sent * spc
    if offset >= len(s.pcm):
        raise HTTPException(400, "всё аудио отправлено — выполните FULL FINALIZATION")
    n = min(spc, len(s.pcm) - offset)
    cell = np.zeros(spc, dtype=np.int16)
    cell[:n] = s.pcm[offset:offset + n]
    actual_ms = int(n * 1000 / SAMPLE_RATE)
    _init_triton(s)
    t0 = time.monotonic()
    resp = await asyncio.to_thread(
        s.triton.send, s.sequence_id, cell, _build_marks(actual_ms),
        s.chunk_index, s.chunk_index == 0, False, "False",
        f"test-{s.session_id}-cell-{s.chunk_index}")
    ack_ms = int((time.monotonic() - t0) * 1000)
    cid = s.chunk_index
    s.chunk_index += 1
    s.cells_sent += 1
    segs = _parse_segments(resp)
    _do_debug(s, f"cell-{cid}", "audio", cid, ack_ms,
              segments=segs if segs else None)
    partial_done = False
    if _auto_partial_due(s):
        await _do_partial(s, auto=True)
        partial_done = True
    return {
        "cell": s.cells_sent,
        "chunk_id": cid,
        "ack_ms": ack_ms,
        "actual_ms": actual_ms,
        "is_last_cell": s.cells_sent >= s.audio_cells_total,
        "partial_after": partial_done,
        "latest_snapshot_segments": (
            s.partial_snapshots[-1]["segments"]
            if s.partial_snapshots else segs),
        "state": s.state,
    }


async def _do_partial(s: TestSession, auto: bool = False):
    """PARTIAL-сигнал: тишина 1 чанк, пустые marks, состояние сохраняется."""
    silence = np.zeros(s.chunk_ms * SAMPLE_RATE // 1000, dtype=np.int16)
    _init_triton(s)
    t0 = time.monotonic()
    resp = await asyncio.to_thread(
        s.triton.send, s.sequence_id, silence, _build_marks(0),
        s.chunk_index, False, False, "True",
        f"test-{s.session_id}-partial-{len(s.partial_snapshots)}")
    ack_ms = int((time.monotonic() - t0) * 1000)
    cid = s.chunk_index
    s.chunk_index += 1
    segs = _parse_segments(resp)
    snap = {
        "n": len(s.partial_snapshots) + 1,
        "t": round(time.time(), 3),
        "auto": auto,
        "chunk_id": cid,
        "ack_ms": ack_ms,
        "speakers": sorted({x["speaker_id"] for x in segs}),
        "segments": segs,
    }
    s.partial_snapshots.append(snap)
    _do_debug(s, f"partial-{snap['n']}", "partial", cid, ack_ms, segs)
    return snap


@router.post("/api/session/{sid}/send-chunk")
async def send_chunk(sid: str):
    s = _get(sid)
    async with s.lock:
        if s.state not in ("running", "paused"):
            raise HTTPException(409, f"тест не запущен (state={s.state})")
        if s.state == "paused":
            s.state = "running"
        return await _send_cell(s)


@router.post("/api/session/{sid}/partial")
async def manual_partial(sid: str):
    """Ручной PARTIAL (режим manual или любой — сериализуется lock'ом)."""
    s = _get(sid)
    async with s.lock:
        if s.state not in ("running", "paused"):
            raise HTTPException(409, f"тест не запущен (state={s.state})")
        snap = await _do_partial(s, auto=False)
    return snap


@router.post("/api/session/{sid}/full")
async def full_finalization(sid: str):
    """FULL: sequence_end=True — финальный снапшот, состояние освобождается."""
    s = _get(sid)
    async with s.lock:
        if s.state not in ("running", "paused"):
            raise HTTPException(409, f"тест не запущен (state={s.state})")
        if s.partial_mode == "before_full" and not s.partial_snapshots:
            await _do_partial(s, auto=True)
        silence = np.zeros(s.chunk_ms * SAMPLE_RATE // 1000, dtype=np.int16)
        _init_triton(s)
        t0 = time.monotonic()
        resp = await asyncio.to_thread(
            s.triton.send, s.sequence_id, silence, _build_marks(0),
            s.chunk_index, False, True, "False",
            f"test-{s.session_id}-full")
        ack_ms = int((time.monotonic() - t0) * 1000)
        cid = s.chunk_index
        s.chunk_index += 1
        s.final_segments = _parse_segments(resp)
        s.state = "finalized"
        _do_debug(s, "full", "full", cid, ack_ms, s.final_segments)
        _persist_result(s)
        return {
            "state": s.state,
            "chunk_id": cid,
            "ack_ms": ack_ms,
            "segments": s.final_segments,
            "speakers": sorted({x["speaker_id"] for x in s.final_segments}),
            "partial_count": len(s.partial_snapshots),
        }


@router.post("/api/session/{sid}/cancel")
async def cancel(sid: str):
    """Отмена с очисткой sequence: sequence_end=True (без сохранения результата)."""
    s = _get(sid)
    async with s.lock:
        if s.state in ("finalized", "cancelled"):
            return {"state": s.state}
        cleaned = False
        try:
            if s.triton is not None and s.chunk_index > 0:
                silence = np.zeros(
                    s.chunk_ms * SAMPLE_RATE // 1000, dtype=np.int16)
                await asyncio.to_thread(
                    s.triton.send, s.sequence_id, silence, _build_marks(0),
                    s.chunk_index, False, True, "False",
                    f"test-{s.session_id}-cancel")
                s.chunk_index += 1
                cleaned = True
        except Exception as e:
            _do_debug(s, "cancel", "cancel-cleanup", s.chunk_index,
                      0, error=str(e))
        s.state = "cancelled"
        s.error = None
        _do_debug(s, "cancel", "cancel", s.chunk_index, 0,
                  error=None if cleaned else "sequence не очищена")
        _persist_result(s)
        return {"state": s.state, "sequence_cleaned": cleaned}


def _persist_result(s: TestSession):
    try:
        STORAGE.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": s.session_id,
            "model": MODEL_NAME,
            "triton_url": TRITON_URL,
            "filename": s.filename,
            "file_info": s.file_info,
            "chunk_ms": s.chunk_ms,
            "partial_mode": s.partial_mode,
            "sequence_id": s.sequence_id,
            "state": s.state,
            "partial_snapshots": s.partial_snapshots,
            "final_segments": s.final_segments,
            "debug": s.debug,
        }
        (STORAGE / f"{s.session_id}_result.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        logger.warning("diarization-test: persist failed: %s", e)


def _export_json(s: TestSession) -> dict:
    return {
        "session_id": s.session_id,
        "model": MODEL_NAME,
        "filename": s.filename,
        "file_info": s.file_info,
        "chunk_ms": s.chunk_ms,
        "partial_mode": s.partial_mode,
        "state": s.state,
        "cells_sent": s.cells_sent,
        "partial_snapshots": s.partial_snapshots,
        "final_segments": s.final_segments,
    }


def _export_txt(s: TestSession) -> str:
    L = []
    L.append(f"diarization_online_pf test — {s.filename}")
    L.append(f"session={s.session_id} model={MODEL_NAME} triton={TRITON_URL}")
    L.append(f"chunk_ms={s.chunk_ms} partial_mode={s.partial_mode} state={s.state}")
    fi = s.file_info
    L.append(f"file: {fi['duration_ms']} ms, {fi['sample_rate']} Hz, "
             f"{fi['channels']} ch, {fi['sample_width_bytes']} B/sample")
    L.append(f"cells sent: {s.cells_sent}/{s.audio_cells_total}, "
             f"model cells (incl. signals): {s.chunk_index}")
    L.append("")
    for p in s.partial_snapshots:
        L.append(f"PARTIAL #{p['n']} (auto={p['auto']}, chunk_id={p['chunk_id']}, "
                 f"ack={p['ack_ms']} ms) speakers={p['speakers']}")
        for x in p["segments"]:
            L.append(f"  {x['start_ms']:>9} - {x['end_ms']:>9} ms  speaker {x['speaker_id']}")
    L.append("")
    if s.state == "finalized":
        L.append(f"FINAL ({len(s.final_segments)} segments):")
        for x in s.final_segments:
            L.append(f"  {x['start_ms']:>9} - {x['end_ms']:>9} ms  speaker {x['speaker_id']}")
    return "\n".join(L) + "\n"


def _json_response(obj) -> Response:
    return Response(
        content=json.dumps(obj, ensure_ascii=False, indent=1),
        media_type="application/json")


@router.get("/api/session/{sid}/export.json")
async def export_json(sid: str):
    s = _get(sid)
    return _json_response(_export_json(s))


@router.get("/api/session/{sid}/export.txt")
async def export_txt(sid: str):
    s = _get(sid)
    return Response(content=_export_txt(s), media_type="text/plain; charset=utf-8")


@router.get("/api/session/{sid}/export.debug")
async def export_debug(sid: str):
    s = _get(sid)
    return _json_response({
        "session_id": s.session_id,
        "sequence_id": s.sequence_id,
        "triton_url": TRITON_URL,
        "model": MODEL_NAME,
        "requests": s.debug,
    })


PAGE_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Диаризация PF — тест модели</title>
<style>
  :root {
    --bg: #f3f5f9; --card: #ffffff; --text: #1f2937; --muted: #6b7280;
    --border: #e5e7eb; --accent: #2563eb; --accent-hover: #1d4ed8;
    --ok: #16a34a; --warn: #d97706; --error-bg: #fef2f2; --error-text: #b91c1c;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif; padding: 24px; }
  .wrap { max-width: 980px; margin: 0 auto; display: flex;
    flex-direction: column; gap: 16px; }
  .card { background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 18px 20px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  h2 { font-size: 15px; margin: 0 0 12px; color: var(--muted);
    text-transform: uppercase; letter-spacing: .04em; }
  .muted { color: var(--muted); font-size: 13px; }
  .row { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
  button { background: var(--accent); border: none; color: #fff;
    border-radius: 8px; padding: 9px 16px; font-size: 14px; cursor: pointer; }
  button:hover:not(:disabled) { background: var(--accent-hover); }
  button:disabled { opacity: .45; cursor: default; }
  button.secondary { background: #6b7280; }
  button.danger { background: #dc2626; }
  button.ok { background: var(--ok); }
  select, input[type=number], input[type=file] {
    border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px;
    font-size: 14px; background: #fff; }
  .bar { height: 10px; background: var(--border); border-radius: 6px;
    overflow: hidden; }
  .bar > div { height: 100%; background: var(--accent); width: 0;
    transition: width .2s; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 600; }
  .err { background: var(--error-bg); color: var(--error-text);
    border: 1px solid #fecaca; border-radius: 8px; padding: 10px 14px;
    font-size: 14px; display: none; }
  .okbox { background: #f0fdf4; color: var(--ok); border: 1px solid #bbf7d0;
    border-radius: 8px; padding: 10px 14px; font-size: 14px; display: none; }
  .kv { display: flex; flex-wrap: wrap; gap: 8px 18px; font-size: 14px; }
  .kv b { font-weight: 600; }
  .spk { display: inline-block; background: #e0e7ff; color: #3730a3;
    border-radius: 10px; padding: 2px 10px; margin: 2px 4px 2px 0;
    font-size: 13px; }
  details { margin: 6px 0; }
  summary { cursor: pointer; font-size: 13px; color: var(--muted); }
  a.dl { color: var(--accent); text-decoration: none; font-size: 14px; }
  a.dl:hover { text-decoration: underline; }
  .hidden { display: none; }
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>Тест модели diarization_online_pf</h1>
    <div class="muted">PARTIAL FINALIZATION + стабильные speaker ID.
      Запросы — напрямую в Triton gRPC, paced (один в полёте).
      Без Kafka / PostgreSQL / S3.</div>
  </div>

  <div class="card">
    <h2>1. Аудио</h2>
    <div class="row">
      <input type="file" id="file" accept=".wav,audio/wav">
      <button id="btnUpload">Загрузить WAV</button>
    </div>
    <div id="fileInfo" class="kv hidden" style="margin-top:12px"></div>
  </div>

  <div class="card" id="settingsCard">
    <h2>2. Настройки</h2>
    <div class="row">
      <label>Размер чанка, мс:
        <input type="number" id="chunkMs" value="3000" min="250" max="10000" step="100">
      </label>
      <label>PARTIAL:
        <select id="partialMode">
          <option value="none">не выполнять</option>
          <option value="each">после каждого чанка</option>
          <option value="5" selected>каждые 5 чанков</option>
          <option value="10">каждые 10 чанков</option>
          <option value="20">каждые 20 чанков</option>
          <option value="manual">только вручную</option>
          <option value="before_full">только перед FULL</option>
        </select>
      </label>
    </div>
  </div>

  <div class="card" id="controlCard">
    <h2>3. Управление</h2>
    <div class="row">
      <button id="btnStart">Начать обработку</button>
      <button id="btnPause" class="secondary" disabled>Пауза</button>
      <button id="btnPartial" disabled>Ручной PARTIAL</button>
      <button id="btnFull" class="ok" disabled>FULL FINALIZATION</button>
      <button id="btnCancel" class="danger" disabled>Отмена</button>
    </div>
    <div class="row" style="margin-top:14px">
      <div style="flex:1;min-width:220px">
        <div class="row" style="justify-content:space-between">
          <span class="muted" id="progLabel">ожидание</span>
          <span class="muted" id="durLabel"></span>
        </div>
        <div class="bar" style="margin-top:6px"><div id="prog"></div></div>
      </div>
    </div>
    <div class="row" style="margin-top:10px">
      <span class="muted">Speaker ID: <b id="spkCount">0</b></span>
      <span id="spkList"></span>
      <span class="muted" id="sentLabel"></span>
    </div>
    <div id="errBox" class="err"></div>
    <div id="okBox" class="okbox"></div>
  </div>

  <div class="card hidden" id="segCard">
    <h2>4. Сегменты (последний снапшот / финал)</h2>
    <div class="muted" id="segCaption"></div>
    <table style="margin-top:8px">
      <thead><tr><th>#</th><th>Начало</th><th>Конец</th><th>D, мс</th>
        <th>Speaker</th></tr></thead>
      <tbody id="segBody"></tbody>
    </table>
  </div>

  <div class="card hidden" id="histCard">
    <h2>5. История PARTIAL</h2>
    <div id="hist"></div>
  </div>

  <div class="card hidden" id="exportCard">
    <h2>6. Экспорт</h2>
    <div class="row">
      <a class="dl" id="dlJson" href="#">JSON (результат)</a>
      <a class="dl" id="dlTxt" href="#">TXT</a>
      <a class="dl" id="dlDebug" href="#">Debug JSON (запросы / ACK)</a>
    </div>
  </div>
</div>

<script>
var S = { sid: null, total: 0, running: false, paused: false, started: false };
var loopToken = 0;
function $(id) { return document.getElementById(id); }
function fmt(ms) {
  var s = ms / 1000, m = Math.floor(s / 60), sec = s - m * 60;
  var t = sec.toFixed(3);
  return m + ':' + (t.length < 4 ? '0' : '') + t;
}
function setErr(msg) {
  $('errBox').textContent = msg;
  $('errBox').style.display = msg ? 'block' : 'none';
  $('okBox').style.display = 'none';
}
function setOk(msg) {
  $('okBox').textContent = msg;
  $('okBox').style.display = msg ? 'block' : 'none';
  $('errBox').style.display = 'none';
}
async function api(path, body) {
  var opt = { method: 'GET' };
  if (body) {
    opt.method = 'POST';
    if (body instanceof FormData) { opt.body = body; }
    else {
      opt.headers = { 'Content-Type': 'application/json' };
      opt.body = JSON.stringify(body);
    }
  }
  var r = await fetch('/diarization-test/api/' + path, opt);
  var j = null;
  try { j = await r.json(); } catch (e) {}
  if (!r.ok) {
    var d = (j && (j.detail || j.message)) || ('HTTP ' + r.status);
    if (typeof d !== 'string') d = JSON.stringify(d);
    throw new Error(d);
  }
  return j;
}
function updateButtons() {
  var st = S.state;
  $('btnUpload').disabled = S.started;
  $('btnStart').disabled = !S.started || S.running;
  $('btnPause').disabled = !S.running;
  $('btnPause').textContent = S.paused ? 'Продолжить' : 'Пауза';
  $('btnPartial').disabled = !S.running;
  $('btnFull').disabled = !S.running;
  $('btnCancel').disabled = !S.running;
}
function renderStatus(st, caption) {
  $('prog').style.width = Math.min(100, st.cells_sent / st.audio_cells_total * 100) + '%';
  $('progLabel').textContent = 'чанков: ' + st.cells_sent + ' / ' + st.audio_cells_total +
    (st.partial_count ? '  |  PARTIAL: ' + st.partial_count : '');
  $('durLabel').textContent = fmt(st.sent_audio_ms) + ' / ' + fmt(st.duration_ms);
  $('sentLabel').textContent = 'модельных ячеек (с сигналами): ' + st.chunk_index;
  $('spkCount').textContent = st.speakers.length;
  $('spkList').innerHTML = st.speakers.map(function (s) {
    return '<span class="spk">#' + s + '</span>'; }).join('');
  var rows = st.latest_segments || [];
  $('segCard').classList.remove('hidden');
  $('segCaption').textContent = caption + ' (' + rows.length + ' сегментов)';
  $('segBody').innerHTML = rows.map(function (x, i) {
    return '<tr><td>' + (i + 1) + '</td><td>' + fmt(x.start_ms) + '</td><td>' +
      fmt(x.end_ms) + '</td><td>' + (x.end_ms - x.start_ms) + '</td><td>#' +
      x.speaker_id + '</td></tr>'; }).join('');
  if (st.partial_count) {
    $('histCard').classList.remove('hidden');
    var h = [];
    for (var i = st.partial_count - 1; i >= 0; i--) {
      var p = st.partial_snapshots[i];
      h.push('<details><summary>PARTIAL #' + p.n + (p.auto ? ' (авто)' : ' (ручной)') +
        ' — ' + p.segments.length + ' сегм., speakers ' + p.speakers.join(',') +
        '</summary><table><tbody>' + p.segments.map(function (x) {
          return '<tr><td>' + fmt(x.start_ms) + '</td><td>' + fmt(x.end_ms) +
            '</td><td>#' + x.speaker_id + '</td></tr>'; }).join('') +
        '</tbody></table></details>');
    }
    $('hist').innerHTML = h.join('');
  }
}
function renderFull(res) {
  setOk('FULL FINALIZATION выполнен: ' + res.segments.length + ' сегментов, ' +
    res.speakers.length + ' speaker(s). Sequence освобождена.');
  $('segCard').classList.remove('hidden');
  $('segCaption').textContent = 'ФИНАЛЬНЫЙ РЕЗУЛЬТАТ (' + res.segments.length + ' сегментов)';
  $('segBody').innerHTML = res.segments.map(function (x, i) {
    return '<tr><td>' + (i + 1) + '</td><td>' + fmt(x.start_ms) + '</td><td>' +
      fmt(x.end_ms) + '</td><td>' + (x.end_ms - x.start_ms) + '</td><td>#' +
      x.speaker_id + '</td></tr>'; }).join('');
  var base = '/diarization-test/api/' + S.sid + '/';
  $('dlJson').href = base + 'export.json';
  $('dlTxt').href = base + 'export.txt';
  $('dlDebug').href = base + 'export.debug';
  $('exportCard').classList.remove('hidden');
}
async function loop() {
  var my = ++loopToken;
  while (S.running && !S.paused && my === loopToken) {
    var st = null;
    try { st = await api(S.sid + ''); }
    catch (e) { S.running = false; setErr(e.message); break; }
    if (st.cells_sent >= st.audio_cells_total) break;
    try {
      var r = await api(S.sid + '/send-chunk', {});
      renderStatus(st, r.partial_after ? 'снапшот после PARTIAL' : 'аудио (без PARTIAL)');
      if (r.is_last_cell) break;
    } catch (e) {
      S.running = false;
      setErr('Ошибка: ' + e.message + ' — тест остановлен. Используйте Отмена.');
      break;
    }
  }
  if (my !== loopToken) return;
  if (!S.paused) { S.running = false; }
  updateButtons();
}
$('btnUpload').onclick = async function () {
  var f = $('file').files[0];
  if (!f) return;
  setErr(''); setOk('');
  this.disabled = true;
  try {
    var fd = new FormData();
    fd.append('file', f);
    var r = await api('upload', fd);
    S.sid = r.session_id;
    S.total = r.audio_cells_total;
    S.started = true;
    S.state = 'uploaded';
    var fi = r.file;
    $('fileInfo').classList.remove('hidden');
    $('fileInfo').innerHTML =
      '<span>Файл: <b>' + f.name + '</b></span>' +
      '<span>Длительность: <b>' + fmt(fi.duration_ms) + '</b></span>' +
      '<span>Частота: <b>' + fi.sample_rate + ' Hz</b>' +
      (fi.resampled ? ' → 16000' : '') + '</span>' +
      '<span>Каналов: <b>' + fi.channels + '</b>' +
      (fi.mixed_to_mono ? ' → mono' : '') + '</span>' +
      '<span>Разрядность: <b>' + fi.sample_width_bytes * 8 + ' bit</b></span>' +
      '<span>Ячеек: <b>' + r.audio_cells_total + '</b></span>';
    setOk('WAV загружен и преобразован (mono PCM 16 kHz). Модель: ' + r.model + '.');
    updateButtons();
  } catch (e) {
    this.disabled = false;
    setErr('Загрузка: ' + e.message);
  }
};
$('btnStart').onclick = async function () {
  setErr(''); setOk('');
  this.disabled = true;
  try {
    var body = {
      chunk_ms: parseInt($('chunkMs').value, 10),
      partial_mode: $('partialMode').value
    };
    var r = await api(S.sid + '/config', body);
    S.total = r.audio_cells_total;
    S.running = true;
    S.paused = false;
    S.state = 'running';
    updateButtons();
    loop();
  } catch (e) {
    this.disabled = false;
    setErr('Запуск: ' + e.message);
  }
};
$('btnPause').onclick = function () {
  if (!S.running) return;
  S.paused = !S.paused;
  if (!S.paused) { S.running = true; updateButtons(); loop(); }
  updateButtons();
};
$('btnPartial').onclick = async function () {
  try {
    var p = await api(S.sid + '/partial', {});
    setOk('PARTIAL #' + p.n + ' получен: ' + p.segments.length + ' сегментов, ' +
      'speakers ' + p.speakers.join(',') + '.');
    var st = await api(S.sid + '');
    renderStatus(st, 'снапшот после ручного PARTIAL');
  } catch (e) { setErr('PARTIAL: ' + e.message); }
};
$('btnFull').onclick = async function () {
  setErr('');
  this.disabled = true;
  loopToken++;
  try {
    var res = await api(S.sid + '/full', {});
    S.running = false;
    S.paused = false;
    S.state = 'finalized';
    renderFull(res);
    updateButtons();
  } catch (e) {
    this.disabled = false;
    setErr('FULL: ' + e.message);
  }
};
$('btnCancel').onclick = async function () {
  loopToken++;
  try {
    var r = await api(S.sid + '/cancel', {});
    S.running = false;
    S.paused = false;
    S.state = 'cancelled';
    setOk('Тест отменён. Sequence ' +
      (r.sequence_cleaned ? 'освобождена (sequence_end).') :
      'не освобождена (модель очистит по idle-timeout).');
    updateButtons();
  } catch (e) { setErr('Отмена: ' + e.message); }
};
</script>
</body>
</html>
"""


def render_page() -> HTMLResponse:
    info = ""
    if TRITON_IMPORT_ERROR:
        info = ("<div class=err style='display:block'>gRPC-зависимости не "
                "установлены: " + TRITON_IMPORT_ERROR + "</div>")
    return HTMLResponse(
        PAGE_HTML.replace("<div class=\"wrap\">",
                          "<div class=\"wrap\">" + info, 1))
