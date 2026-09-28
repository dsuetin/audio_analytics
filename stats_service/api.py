from datetime import date, datetime
from pathlib import Path
import os
import io
import wave
import tempfile
import boto3
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response

app = FastAPI(title="Stats Service")

REPORT_DIR = Path(
    os.getenv(
        "REPORT_OUTPUT_DIR",
        "/reports",
    )
)

S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "http://localhost:9000")
S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID", "minioadmin")
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY", "minioadmin")
S3_BUCKET = os.getenv("S3_BUCKET", "audio-sessions")


def get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=S3_ACCESS_KEY_ID,
        aws_secret_access_key=S3_SECRET_ACCESS_KEY,
    )


def list_sessions_for_store(s3_client, date_str: str, store_name: str):
    """List all sessions for a store on a given date"""
    date_prefix = f"audio/{date_str}"
    sessions = []

    # Нормализуем название магазина (убираем пробелы и приводим к нижнему регистру)
    store_normalized = store_name.replace(" ", "").lower()

    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=date_prefix):
        if "Contents" in page:
            for obj in page["Contents"]:
                key = obj["Key"]
                parts = key.split("/")
                if len(parts) >= 2:
                    session = parts[1]
                    session_normalized = session.replace(" ", "").lower()
                    # Проверяем частичное совпадение
                    if store_normalized in session_normalized or session_normalized in store_normalized:
                        if session not in sessions:
                            sessions.append(session)

    return sorted(sessions)


def get_session_time_minutes(session_id: str):
    """Extract time from session_id and convert to minutes"""
    try:
        time_part = session_id.split("-")[1]
        hours = int(time_part[:2])
        minutes = int(time_part[2:4])
        return hours * 60 + minutes
    except (IndexError, ValueError):
        return None


def download_session_chunks(s3_client, session_id: str) -> bytes:
    """Download all chunks for a session, extract PCM from each, and concatenate"""
    session_prefix = f"audio/{session_id}/"
    session_files = []

    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=session_prefix):
        if "Contents" in page:
            session_files.extend([obj["Key"] for obj in page["Contents"]])

    session_files = sorted(session_files)
    all_audio_data = b""

    for chunk_key in session_files:
        try:
            obj = s3_client.get_object(Bucket=S3_BUCKET, Key=chunk_key)
            chunk_data = obj["Body"].read()
            if len(chunk_data) > 44 and chunk_data[:4] == b"RIFF":
                pcm_data = chunk_data[44:]
            else:
                pcm_data = chunk_data
            all_audio_data += pcm_data
        except Exception:
            pass

    return all_audio_data


def merge_audio_sessions(session_data_list: list[bytes], sample_rate: int = 16000, num_channels: int = 1, sample_width: int = 2) -> io.BytesIO:
    """Merge multiple audio sessions into one WAV file without silence padding"""
    buffer = io.BytesIO()

    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(num_channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)

        for session_data in session_data_list:
            if session_data:
                wf.writeframes(session_data)

    buffer.seek(0)
    return buffer


INDEX_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Статистика — отчёт за день</title>
<style>
  :root {
    --bg: #f3f5f9;
    --card: #ffffff;
    --text: #1f2937;
    --muted: #6b7280;
    --border: #e5e7eb;
    --accent: #2563eb;
    --accent-hover: #1d4ed8;
    --error-bg: #fef2f2;
    --error-border: #fecaca;
    --error-text: #b91c1c;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
      "Helvetica Neue", Arial, sans-serif;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
  }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    box-shadow: 0 1px 3px rgba(16, 24, 40, 0.06), 0 8px 24px rgba(16, 24, 40, 0.06);
    width: 100%;
    max-width: 420px;
    padding: 40px 36px;
  }
  h1 {
    margin: 0 0 8px;
    font-size: 24px;
    font-weight: 650;
    letter-spacing: -0.02em;
  }
  .subtitle {
    margin: 0 0 28px;
    color: var(--muted);
    font-size: 15px;
    line-height: 1.5;
  }
  label {
    display: block;
    margin: 0 0 8px;
    font-size: 14px;
    font-weight: 500;
  }
  input[type=date] {
    width: 100%;
    padding: 11px 12px;
    font-size: 15px;
    color: var(--text);
    background: #fff;
    border: 1px solid var(--border);
    border-radius: 10px;
    outline: none;
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  input[type=date]:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.15);
  }
  button {
    width: 100%;
    margin-top: 20px;
    padding: 12px 16px;
    font-size: 15px;
    font-weight: 600;
    color: #fff;
    background: var(--accent);
    border: none;
    border-radius: 10px;
    cursor: pointer;
    transition: background 0.15s;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
  }
  button:hover:not(:disabled) { background: var(--accent-hover); }
  button:disabled { opacity: 0.7; cursor: default; }
  .spinner {
    width: 16px;
    height: 16px;
    border: 2px solid rgba(255, 255, 255, 0.4);
    border-top-color: #fff;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .alert {
    display: none;
    margin-top: 20px;
    padding: 12px 14px;
    font-size: 14px;
    line-height: 1.45;
    color: var(--error-text);
    background: var(--error-bg);
    border: 1px solid var(--error-border);
    border-radius: 10px;
  }
  .alert.visible { display: block; }
  .hint {
    margin-top: 18px;
    font-size: 13px;
    color: var(--muted);
  }
  .nav-link {
    display: block;
    margin-top: 12px;
    padding: 8px 12px;
    text-align: center;
    color: var(--accent);
    text-decoration: none;
    border-radius: 8px;
    transition: background 0.15s;
  }
  .nav-link:hover { background: rgba(37, 99, 235, 0.1); }
</style>
</head>
<body>
  <main class="card">
    <h1>Отчёт за день</h1>
    <p class="subtitle">Выберите дату — файл отчёта будет сформирован и скачан автоматически.</p>

    <form id="report-form">
      <label for="report-date">Дата отчёта</label>
      <input type="date" id="report-date" name="report_date" required>

      <button type="submit" id="submit-btn">
        <span class="spinner" id="spinner" hidden></span>
        <span id="btn-label">Скачать отчёт</span>
      </button>

      <div class="alert" id="alert" role="alert"></div>
    </form>

    <p class="hint">Отчёт включает все разговоры за выбранную дату (файл Excel).</p>
    <a href="/audio" class="nav-link">Скачать аудио по магазину</a>
  </main>

<script>
(function () {
  var form = document.getElementById("report-form");
  var input = document.getElementById("report-date");
  var button = document.getElementById("submit-btn");
  var label = document.getElementById("btn-label");
  var spinner = document.getElementById("spinner");
  var alertBox = document.getElementById("alert");
  var pending = false;

  var yesterday = new Date(Date.now() - 86400000);
  input.value = yesterday.toISOString().slice(0, 10);
  input.max = new Date().toISOString().slice(0, 10);

  function setLoading(loading) {
    pending = loading;
    button.disabled = loading;
    spinner.hidden = !loading;
    label.textContent = loading ? "Загрузка отчёта..." : "Скачать отчёт";
  }

  function showError(message) {
    alertBox.textContent = message;
    alertBox.classList.add("visible");
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    if (pending) {
      return;
    }

    var dateValue = input.value;
    if (!dateValue) {
      showError("Выберите дату отчёта.");
      return;
    }

    alertBox.classList.remove("visible");
    setLoading(true);

    try {
      var response = await fetch(
        "/reports/daily?report_date=" + encodeURIComponent(dateValue)
      );

      if (!response.ok) {
        var detail = "report generation failed";
        try {
          var errBody = await response.json();
          if (errBody && errBody.detail) {
            detail = errBody.detail;
          }
        } catch (e) {
          // тело не JSON — используем сообщение по умолчанию
        }
        throw new Error(detail);
      }

      var contentDisposition = response.headers.get("Content-Disposition") || "";
      var fileName = contentDisposition
        .split(";")
        .map(function (part) { return part.trim(); })
        .find(function (part) { return part.indexOf("filename=") === 0; });

      if (fileName) {
        fileName = fileName.substring("filename=".length).replace(/^"|"$/g, "");
      }

      var blob = await response.blob();
      var url = URL.createObjectURL(blob);
      var link = document.createElement("a");
      link.href = url;
      link.download = fileName || ("transcript_report_" + dateValue + ".xlsx");
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (error) {
      showError("Не удалось загрузить отчёт. Попробуйте ещё раз.");
    } finally {
      setLoading(false);
    }
  });
})();
</script>
</body>
</html>
"""

AUDIO_PAGE_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Скачать аудио — Audio Analytics</title>
<style>
  :root {
    --bg: #f3f5f9;
    --card: #ffffff;
    --text: #1f2937;
    --muted: #6b7280;
    --border: #e5e7eb;
    --accent: #2563eb;
    --accent-hover: #1d4ed8;
    --error-bg: #fef2f2;
    --error-border: #fecaca;
    --error-text: #b91c1c;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
      "Helvetica Neue", Arial, sans-serif;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
  }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    box-shadow: 0 1px 3px rgba(16, 24, 40, 0.06), 0 8px 24px rgba(16, 24, 40, 0.06);
    width: 100%;
    max-width: 480px;
    padding: 40px 36px;
  }
  h1 {
    margin: 0 0 8px;
    font-size: 24px;
    font-weight: 650;
    letter-spacing: -0.02em;
  }
  .subtitle {
    margin: 0 0 28px;
    color: var(--muted);
    font-size: 15px;
    line-height: 1.5;
  }
  label {
    display: block;
    margin: 0 0 8px;
    font-size: 14px;
    font-weight: 500;
  }
  input[type="text"],
  input[type="datetime-local"] {
    width: 100%;
    padding: 11px 12px;
    font-size: 15px;
    color: var(--text);
    background: #fff;
    border: 1px solid var(--border);
    border-radius: 10px;
    outline: none;
    transition: border-color 0.15s, box-shadow 0.15s;
    margin-bottom: 16px;
  }
  input[type="text"]:focus,
  input[type="datetime-local"]:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.15);
  }
  button {
    width: 100%;
    margin-top: 20px;
    padding: 12px 16px;
    font-size: 15px;
    font-weight: 600;
    color: #fff;
    background: var(--accent);
    border: none;
    border-radius: 10px;
    cursor: pointer;
    transition: background 0.15s;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
  }
  button:hover:not(:disabled) { background: var(--accent-hover); }
  button:disabled { opacity: 0.7; cursor: default; }
  .spinner {
    width: 16px;
    height: 16px;
    border: 2px solid rgba(255, 255, 255, 0.4);
    border-top-color: #fff;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .alert {
    display: none;
    margin-top: 20px;
    padding: 12px 14px;
    font-size: 14px;
    line-height: 1.45;
    color: var(--error-text);
    background: var(--error-bg);
    border: 1px solid var(--error-border);
    border-radius: 10px;
  }
  .alert.visible { display: block; }
  .hint {
    margin-top: 18px;
    font-size: 13px;
    color: var(--muted);
  }
  .nav-link {
    display: block;
    margin-top: 12px;
    padding: 8px 12px;
    text-align: center;
    color: var(--accent);
    text-decoration: none;
    border-radius: 8px;
    transition: background 0.15s;
  }
  .nav-link:hover { background: rgba(37, 99, 235, 0.1); }
  .status {
    margin-top: 16px;
    padding: 10px 12px;
    font-size: 14px;
    border-radius: 8px;
    display: none;
  }
  .status.loading {
    display: block;
    background: #eff6ff;
    color: #1e40af;
    border: 1px solid #bfdbfe;
  }
  .status.success {
    display: block;
    background: #f0fdf4;
    color: #166534;
    border: 1px solid #bbf7d0;
  }
</style>
</head>
<body>
  <main class="card">
    <h1>Скачать аудио</h1>
    <p class="subtitle">Укажите магазин и временной интервал для выгрузки аудио.</p>

    <form id="audio-form">
      <label for="store">Магазин</label>
      <input type="text" id="store" name="store" placeholder="Например: Минводы" required>

      <label for="start_time">Время начала</label>
      <input type="datetime-local" id="start_time" name="start_time" required>

      <label for="end_time">Время окончания</label>
      <input type="datetime-local" id="end_time" name="end_time" required>

      <button type="submit" id="submit-btn">
        <span class="spinner" id="spinner" hidden></span>
        <span id="btn-label">Скачать аудио</span>
      </button>

      <div class="alert" id="alert" role="alert"></div>
      <div class="status" id="status"></div>
    </form>

    <p class="hint">Аудио будет скачано в формате WAV. Фактическая длительность может отличаться от календарного интервала.</p>
    <a href="/" class="nav-link">Вернуться к отчётам</a>
  </main>

<script>
(function () {
  var form = document.getElementById("audio-form");
  var storeInput = document.getElementById("store");
  var startTimeInput = document.getElementById("start_time");
  var endTimeInput = document.getElementById("end_time");
  var button = document.getElementById("submit-btn");
  var label = document.getElementById("btn-label");
  var spinner = document.getElementById("spinner");
  var alertBox = document.getElementById("alert");
  var statusBox = document.getElementById("status");
  var pending = false;

  function setLoading(loading) {
    pending = loading;
    button.disabled = loading;
    spinner.hidden = !loading;
    label.textContent = loading ? "Обработка..." : "Скачать аудио";
  }

  function showError(message) {
    alertBox.textContent = message;
    alertBox.classList.add("visible");
    statusBox.className = "status";
  }

  function showStatus(message, type) {
    statusBox.textContent = message;
    statusBox.className = "status " + type;
    alertBox.classList.remove("visible");
  }

  function validateForm() {
    var store = storeInput.value.trim();
    var startValue = startTimeInput.value;
    var endValue = endTimeInput.value;

    if (!store) {
      showError("Укажите название магазина.");
      return false;
    }

    if (!startValue) {
      showError("Укажите время начала.");
      return false;
    }

    if (!endValue) {
      showError("Укажите время окончания.");
      return false;
    }

    var start = new Date(startValue);
    var end = new Date(endValue);

    if (start >= end) {
      showError("Время начала должно быть раньше времени окончания.");
      return false;
    }

    return true;
  }

  function toIsoLocal(value) {
    var dt = new Date(value);
    var offset = dt.getTimezoneOffset();
    var localDt = new Date(dt.getTime() - offset * 60 * 1000);
    return localDt.toISOString().replace("\\.000Z$", "");
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    if (pending) {
      return;
    }

    if (!validateForm()) {
      return;
    }

    alertBox.classList.remove("visible");
    statusBox.className = "status";
    setLoading(true);

    try {
      var store = storeInput.value.trim();
      var startTimeIso = toIsoLocal(startTimeInput.value);
      var endTimeIso = toIsoLocal(endTimeInput.value);

      var url = "/audio/download?store=" + encodeURIComponent(store)
              + "&start_time=" + encodeURIComponent(startTimeIso)
              + "&end_time=" + encodeURIComponent(endTimeIso);

      showStatus("Загрузка аудио...", "loading");

      var response = await fetch(url);

      if (!response.ok) {
        var detail = "Ошибка при загрузке аудио";
        try {
          var errBody = await response.json();
          if (errBody && errBody.detail) {
            detail = errBody.detail;
          }
        } catch (e) {
          // используем сообщение по умолчанию
        }
        throw new Error(detail);
      }

      var contentDisposition = response.headers.get("Content-Disposition") || "";
      var fileName = "audio.wav";
      var filenameMatch = contentDisposition.match(/filename="?([^";]+)"?/);
      if (filenameMatch && filenameMatch[1]) {
        fileName = filenameMatch[1];
      }

      var blob = await response.blob();
      var url = URL.createObjectURL(blob);
      var link = document.createElement("a");
      link.href = url;
      link.download = fileName;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);

      showStatus("Аудио успешно скачано!", "success");
    } catch (error) {
      showError(error.message || "Не удалось загрузить аудио. Попробуйте ещё раз.");
    } finally {
      setLoading(false);
    }
  });
})();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index_page():
    return INDEX_HTML


@app.get("/audio", response_class=HTMLResponse)
def audio_page():
    return AUDIO_PAGE_HTML


@app.get("/reports/daily")
def generate_report(report_date: date):

    REPORT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    report_date_str = report_date.isoformat()

    final_path = REPORT_DIR / f"final_transcript_report_{report_date_str}.xlsx"

    if not final_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Отчёт за дату {report_date_str} не существует",
        )

    return FileResponse(
        path=final_path,
        filename=final_path.name,
        media_type="application/zip",
    )


@app.get("/audio/download")
def download_audio(
    store: str = Query(..., description="Store name"),
    start_time: datetime = Query(..., description="Start time (ISO format)"),
    end_time: datetime = Query(..., description="End time (ISO format)"),
):
    if not store or not store.strip():
        raise HTTPException(status_code=400, detail="Store name is required")

    if start_time >= end_time:
        raise HTTPException(status_code=400, detail="start_time must be before end_time")

    date_str = start_time.strftime("%Y%m%d")
    start_minutes = start_time.hour * 60 + start_time.minute
    end_minutes = end_time.hour * 60 + end_time.minute

    s3_client = get_s3_client()

    all_sessions = list_sessions_for_store(s3_client, date_str, store)

    filtered_sessions = []
    for session in all_sessions:
        session_minutes = get_session_time_minutes(session)
        if session_minutes is not None and start_minutes <= session_minutes < end_minutes:
            filtered_sessions.append(session)

    if not filtered_sessions:
        raise HTTPException(
            status_code=404,
            detail=f"No audio sessions found for store '{store}' in time range {start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')}",
        )

    all_audio_data = []
    for session_id in filtered_sessions:
        audio_data = download_session_chunks(s3_client, session_id)
        if audio_data:
            all_audio_data.append(audio_data)

    if not all_audio_data:
        raise HTTPException(
            status_code=404,
            detail="No audio data available for the selected sessions",
        )

    audio_buffer = merge_audio_sessions(all_audio_data)
    audio_buffer.seek(0)

    safe_store = store.replace(" ", "_").replace("/", "_").replace("\\", "_")
    filename_ascii = ""
    for char in safe_store:
        if ord(char) < 128:
            filename_ascii += char
        else:
            filename_ascii += "_"
    filename = f"audio_{date_str}_{filename_ascii}_{start_time.strftime('%H-%M')}_to_{end_time.strftime('%H-%M')}.wav"

    return Response(
        content=audio_buffer.read(),
        media_type="audio/wav",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )