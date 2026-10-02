from datetime import date, datetime
from pathlib import Path
import os
import io
import wave
import tempfile
import boto3
import re
import json
from typing import Optional
import pandas as pd

from fastapi import FastAPI, HTTPException, Query, Form
from fastapi.responses import FileResponse, HTMLResponse, Response
from stats_service.stores import get_all_stores

app = FastAPI(title="Stats Service")

REPORT_DIR = Path(
    os.getenv(
        "REPORT_OUTPUT_DIR",
        "/reports",
    )
)

COMMENTS_FILE = REPORT_DIR / "comments.json"
MANUAL_DIALOGS_FILE = REPORT_DIR / "manual_dialogs.json"

S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "http://localhost:9000")
S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID", "minioadmin")
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY", "minioadmin")
S3_BUCKET = os.getenv("S3_BUCKET", "audio-sessions")

REPORT_PATTERN = re.compile(r"final_transcript_report_(\d{4}-\d{2}-\d{2})\.xlsx")


def get_available_dates() -> list[str]:
    """Get list of available report dates from the reports directory"""
    dates = set()
    for filepath in REPORT_DIR.glob("final_transcript_report_*.xlsx"):
        match = REPORT_PATTERN.search(filepath.name)
        if match:
            dates.add(match.group(1))
    return sorted(dates)


def load_report_data(date_str: str) -> Optional[pd.DataFrame]:
    """Load report data for a given date"""
    report_path = REPORT_DIR / f"final_transcript_report_{date_str}.xlsx"
    if not report_path.exists():
        return None
    try:
        return pd.read_excel(report_path)
    except Exception:
        return None


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
  .nav-links {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-top: 12px;
  }
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
    <div class="nav-links">
      <a href="/audio" class="nav-link">Скачать аудио по отчётам</a>
      <a href="/audio/time-range" class="nav-link">Скачать аудио по времени</a>
    </div>
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
<title>Аудио по отчётам — Audio Analytics</title>
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
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
      "Helvetica Neue", Arial, sans-serif;
    padding: 24px;
  }
  .container {
    max-width: 1200px;
    margin: 0 auto;
  }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    box-shadow: 0 1px 3px rgba(16, 24, 40, 0.06), 0 8px 24px rgba(16, 24, 40, 0.06);
    padding: 32px;
    margin-bottom: 24px;
  }
  h1 {
    margin: 0 0 8px;
    font-size: 24px;
    font-weight: 650;
    letter-spacing: -0.02em;
  }
  .subtitle {
    margin: 0 0 24px;
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
  select, input[type="date"] {
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
  select:focus, input[type="date"]:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.15);
  }
  .filters {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
  }
  .filters > div {
    display: flex;
    flex-direction: column;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 16px;
  }
  th, td {
    padding: 12px;
    text-align: left;
    border-bottom: 1px solid var(--border);
  }
  th {
    background: #f9fafb;
    font-weight: 600;
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--muted);
  }
  td {
    font-size: 14px;
  }
  tr:hover {
    background: #f9fafb;
  }
  .btn {
    padding: 8px 16px;
    font-size: 14px;
    font-weight: 600;
    color: #fff;
    background: var(--accent);
    border: none;
    border-radius: 8px;
    cursor: pointer;
    transition: background 0.15s;
    text-decoration: none;
    display: inline-block;
    text-align: center;
  }
  .btn:hover:not(:disabled) { background: var(--accent-hover); }
  .btn:disabled { opacity: 0.7; cursor: not-allowed; }
  .alert {
    display: none;
    padding: 12px 14px;
    font-size: 14px;
    line-height: 1.45;
    color: var(--error-text);
    background: var(--error-bg);
    border: 1px solid var(--error-border);
    border-radius: 10px;
    margin-top: 16px;
  }
  .alert.visible { display: block; }
  .nav-link {
    display: inline-block;
    margin-top: 12px;
    padding: 8px 12px;
    color: var(--accent);
    text-decoration: none;
    border-radius: 8px;
    transition: background 0.15s;
  }
  .nav-link:hover { background: rgba(37, 99, 235, 0.1); }
  .loading {
    text-align: center;
    padding: 40px;
    color: var(--muted);
  }
  .spinner {
    width: 24px;
    height: 24px;
    border: 3px solid rgba(37, 99, 235, 0.2);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin: 0 auto 16px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .no-data {
    text-align: center;
    padding: 40px;
    color: var(--muted);
    font-size: 15px;
  }
  .tooltip-cell {
    position: relative;
    cursor: help;
  }
  .tooltip-cell:hover::after {
    content: attr(data-tooltip);
    position: absolute;
    right: 100%;
    top: 50%;
    transform: translateY(-50%);
    background: linear-gradient(135deg, #f8f9ff 0%, #f0f4ff 100%);
    color: #1a1a2e;
    padding: 20px 24px;
    border-radius: 16px;
    font-size: 15px;
    line-height: 1.7;
    min-width: 350px;
    max-width: 600px;
    white-space: pre-wrap;
    word-wrap: break-word;
    z-index: 1000;
    box-shadow: 0 12px 48px rgba(59, 130, 246, 0.25);
    margin-right: 16px;
    border: 2px solid #e8efff;
  }
  .tooltip-cell:hover::before {
    content: '';
    position: absolute;
    right: 100%;
    top: 50%;
    transform: translateY(-50%);
    border: 12px solid transparent;
    border-left-color: #e8efff;
    margin-right: -12px;
    margin-top: -12px;
    z-index: 999;
  }
  .comment-input {
    width: 100%;
    padding: 8px 12px;
    border: 2px solid #e0e7ff;
    border-radius: 8px;
    font-size: 13px;
    transition: all 0.2s ease;
    background: #fafbff;
  }
  .comment-input:focus {
    outline: none;
    border-color: #3b82f6;
    background: #fff;
    box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
  }
  .comment-input::placeholder {
    color: #94a3b8;
  }
  .add-dialog-btn {
    display: block;
    width: 100%;
    text-align: center;
    padding: 6px;
    margin: 4px 0;
    background: linear-gradient(135deg, #f0f4ff 0%, #e8efff 100%);
    color: #3b82f6;
    border: 2px dashed #c7d2fe;
    border-radius: 8px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 500;
    transition: all 0.2s ease;
  }
  .add-dialog-btn:hover {
    background: linear-gradient(135deg, #e8efff 0%, #dbeafe 100%);
    border-color: #3b82f6;
    transform: translateY(-1px);
  }
  .modal-overlay {
    display: none;
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.5);
    z-index: 10000;
    align-items: center;
    justify-content: center;
  }
  .modal-overlay.active {
    display: flex;
  }
  .modal {
    background: #fff;
    border-radius: 16px;
    padding: 24px;
    width: 90%;
    max-width: 500px;
    max-height: 90vh;
    overflow-y: auto;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
  }
  .modal h2 {
    margin: 0 0 20px;
    color: #1a1a2e;
    font-size: 20px;
  }
  .form-group {
    margin-bottom: 16px;
  }
  .form-group label {
    display: block;
    margin-bottom: 6px;
    color: #374151;
    font-size: 13px;
    font-weight: 500;
  }
  .form-group input,
  .form-group textarea {
    width: 100%;
    padding: 10px 12px;
    border: 2px solid #e5e7eb;
    border-radius: 8px;
    font-size: 14px;
    transition: all 0.2s ease;
    box-sizing: border-box;
  }
  .form-group input:focus,
  .form-group textarea:focus {
    outline: none;
    border-color: #3b82f6;
    box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
  }
  .form-group textarea {
    min-height: 80px;
    resize: vertical;
  }
  .modal-buttons {
    display: flex;
    gap: 12px;
    margin-top: 20px;
  }
  .modal-buttons button {
    flex: 1;
    padding: 12px;
    border: none;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
  }
  .btn-primary {
    background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%);
    color: #fff;
  }
  .btn-primary:hover {
    background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%);
    transform: translateY(-1px);
  }
  .btn-secondary {
    background: #f3f4f6;
    color: #374151;
  }
  .btn-secondary:hover {
    background: #e5e7eb;
  }
  .error-message {
    background: #fef2f2;
    border: 1px solid #fecaca;
    color: #dc2626;
    padding: 12px;
    border-radius: 8px;
    font-size: 13px;
    margin-bottom: 16px;
    display: none;
  }
  .error-message.active {
    display: block;
  }
  .manual-badge {
    display: inline-block;
    background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%);
    color: #fff;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
    margin-left: 4px;
  }
  .delete-btn {
    background: #fee2e2;
    color: #dc2626;
    border: none;
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 11px;
    cursor: pointer;
    transition: all 0.2s ease;
  }
  .delete-btn:hover {
    background: #fecaca;
  }
</style>
</head>
<body>
  <div class="container">
    <div class="card">
      <h1>Аудио по отчётам</h1>
      <p class="subtitle">Выберите дату отчёта, магазин и клиента для просмотра доступных аудиозаписей.</p>

      <div class="filters">
        <div>
          <label for="date-select">Дата отчёта</label>
          <select id="date-select"></select>
        </div>
         <div>
           <label for="store-select">Магазин</label>
           <select id="store-select">
             <option value="all">Все магазины</option>
           </select>
         </div>
       </div>

       <div id="table-container">
         <div class="loading">
           <div class="spinner"></div>
           <div>Выберите дату для загрузки данных</div>
         </div>
       </div>

       <div class="alert" id="alert" role="alert"></div>

        <div class="modal-overlay" id="modal-overlay">
          <div class="modal">
            <h2>Добавить пропущенный диалог</h2>
            <div class="error-message" id="modal-error"></div>
            <form id="manual-dialog-form">
              <div class="form-group">
                <label for="manual-start-time">Начало сессии</label>
                <input type="time" id="manual-start-time" required />
              </div>
              <div class="form-group">
                <label for="manual-end-time">Конец сессии</label>
                <input type="time" id="manual-end-time" required />
              </div>
              <div class="form-group">
                <label for="manual-dialog-type">Цель миссии</label>
                <textarea id="manual-dialog-type" placeholder="Например: Покупка товара" required></textarea>
              </div>
              <div class="form-group">
                <label for="manual-result">Итог</label>
                <textarea id="manual-result" placeholder="Например: Клиент совершил покупку" required></textarea>
              </div>
              <div class="modal-buttons">
                <button type="button" class="btn-secondary" id="modal-cancel">Отмена</button>
                <button type="submit" class="btn-primary">Сохранить</button>
              </div>
            </form>
          </div>
        </div>

      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;">
        <a href="/audio/time-range" class="nav-link">Скачать аудио по времени</a>
        <a href="/" class="nav-link">← Вернуться к отчётам</a>
      </div>
    </div>
  </div>

<script>
(function () {
  var dateSelect = document.getElementById("date-select");
  var storeSelect = document.getElementById("store-select");
  var tableContainer = document.getElementById("table-container");
  var alertBox = document.getElementById("alert");
  var currentData = null;

  function showError(message) {
    alertBox.textContent = message;
    alertBox.classList.add("visible");
  }

  function hideError() {
    alertBox.classList.remove("visible");
  }

  function formatDuration(seconds) {
    var mins = Math.floor(seconds / 60);
    var secs = Math.floor(seconds % 60);
    return mins.toString().padStart(2, "0") + ":" + secs.toString().padStart(2, "0");
  }

  function renderTable(dialogs) {
    if (!dialogs || dialogs.length === 0) {
      tableContainer.innerHTML = '<div class="no-data">Нет данных для отображения</div>';
      return;
    }

    var dateStr = dateSelect.value;
    var storeId = storeSelect.value === "all" ? null : storeSelect.value;
    var html = '<div style="display:flex;flex-direction:column;gap:4px;">';

    for (var i = 0; i < dialogs.length; i++) {
      var d = dialogs[i];
      var isManual = d.is_manual === true;
      
      if (storeId) {
        if (i === 0) {
          // Add button before first dialog
          html += '<button class="add-dialog-btn">+ Добавить диалог</button>';
        } else {
          // Add button between dialogs
          html += '<button class="add-dialog-btn">+ Добавить диалог</button>';
        }
      }
      
      var dialogType = isManual ? d.dialog_type : getDialogTypeLabel(d.dialog_type);
      var saleResult = isManual ? d.result : (d.is_sale ? "Покупка" : "Нет покупки");
      var reasoning = d.model_reasoning || "Нет объяснения";
      var reasoningEscaped = escapeHtml(reasoning);
      var recognitionText = d.recognition_text || "Нет текста";
      var recognitionTextEscaped = escapeHtml(recognitionText);
      var commentKey = dateStr + '_' + d.store_id + '_' + d.client_id;
      var existingComment = userComments[commentKey] || '';
      var existingCommentEscaped = escapeHtml(existingComment);
      
      if (isManual) {
        var startTimeISO = dateStr + 'T' + d.start_time.replace(/:/g, ':');
        var endTimeISO = dateStr + 'T' + d.end_time.replace(/:/g, ':');
        var downloadUrl = "/audio/download?store=" + encodeURIComponent(d.store_id) + "&start_time=" + encodeURIComponent(startTimeISO) + "&end_time=" + encodeURIComponent(endTimeISO);
        html += '<table style="margin:0;border:2px solid #3b82f6;border-radius:8px;overflow:hidden;"><thead style="background:#dbeafe;"><tr>' +
          '<th>Магазин</th>' +
          '<th>Клиент</th>' +
          '<th>Начало</th>' +
          '<th>Конец</th>' +
          '<th>Длительность</th>' +
          '<th>Цель визита</th>' +
          '<th>Результат</th>' +
          '<th>Комментарий</th>' +
          '<th>Аудио</th>' +
          '<th></th>' +
          '</tr></thead><tbody>' +
          '<tr data-comment-key="' + commentKey + '">' +
            '<td>' + escapeHtml(d.store_id) + '</td>' +
            '<td>Manual <span class="manual-badge">Ручной</span></td>' +
            '<td>' + d.start_time + '</td>' +
            '<td>' + d.end_time + '</td>' +
            '<td class="tooltip-cell" data-tooltip="' + recognitionTextEscaped + '">' + formatDuration(0) + '</td>' +
            '<td class="tooltip-cell" data-tooltip="' + reasoningEscaped + '">' + escapeHtml(dialogType) + '</td>' +
            '<td>' + escapeHtml(saleResult) + '</td>' +
            '<td style="width: 300px;">' +
              '<input type="text" class="comment-input" placeholder="Добавить комментарий..." value="' + existingCommentEscaped + '" data-comment-key="' + commentKey + '" />' +
            '</td>' +
            '<td><a href="' + downloadUrl + '" class="btn">▶ Play</a></td>' +
            '<td><button class="delete-btn" data-manual-id="' + d.manual_id + '" onclick="deleteManualDialog(this.dataset.manualId || this.getAttribute(&quot;data-manual-id&quot;))">Удалить</button></td>' +
          '</tr>' +
          '</tbody></table>';
       } else {
         html += '<table style="margin:0;"><thead><tr>' +
           '<th>Магазин</th>' +
           '<th>Клиент</th>' +
           '<th>Начало</th>' +
           '<th>Конец</th>' +
           '<th>Длительность</th>' +
           '<th>Цель визита</th>' +
           '<th>Результат</th>' +
           '<th>Комментарий</th>' +
           '<th>Аудио</th>' +
           '</tr></thead><tbody>' +
           '<tr data-comment-key="' + commentKey + '">' +
             '<td>' + escapeHtml(d.store_id) + '</td>' +
             '<td>' + escapeHtml(d.client_id) + '</td>' +
             '<td>' + d.start_time + '</td>' +
             '<td>' + d.end_time + '</td>' +
             '<td class="tooltip-cell" data-tooltip="' + recognitionTextEscaped + '">' + formatDuration(d.duration_sec) + '</td>' +
             '<td class="tooltip-cell" data-tooltip="' + reasoningEscaped + '">' + dialogType + '</td>' +
             '<td>' + saleResult + '</td>' +
             '<td style="width: 300px;">' +
               '<input type="text" class="comment-input" placeholder="Добавить комментарий..." value="' + existingCommentEscaped + '" data-comment-key="' + commentKey + '" />' +
             '</td>' +
             '<td><button class="btn download-dialog-btn" data-date="' + escapeHtml(dateStr) + '" data-session-ids="' + escapeHtml(d.session_ids.join(",")) + '">Скачать</button></td>' +
           '</tr>' +
           '</tbody></table>';
       }
    }
    
    if (storeId) {
      html += '<button class="add-dialog-btn">+ Добавить диалог</button>';
    }
    
    html += '</div>';
    tableContainer.innerHTML = html;
    
    var inputs = tableContainer.querySelectorAll('.comment-input');
    for (var j = 0; j < inputs.length; j++) {
      inputs[j].addEventListener('blur', function(e) { saveCommentInput(e.target); });
      inputs[j].addEventListener('change', function(e) { saveCommentInput(e.target); });
      inputs[j].addEventListener('keydown', function(e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          saveCommentInput(e.target);
          e.target.blur();
        }
      });
    }
    
    var addButtons = tableContainer.querySelectorAll('.add-dialog-btn');
    for (var k = 0; k < addButtons.length; k++) {
      addButtons[k].addEventListener('click', openAddDialogModal);
    }
    
    var downloadButtons = tableContainer.querySelectorAll('.download-dialog-btn');
    for (var m = 0; m < downloadButtons.length; m++) {
      downloadButtons[m].addEventListener('click', function(e) {
        e.preventDefault();
        var date = this.dataset.date;
        var sessionIds = this.dataset.sessionIds;
        var formData = new FormData();
        formData.append('date_str', date);
        formData.append('session_ids', sessionIds);
        fetch('/api/audio/download-by-dialog', {
          method: 'POST',
          body: formData
        })
        .then(function(r) {
          if (r.ok) {
            return r.blob();
          }
          throw new Error('Download failed: ' + r.status);
        })
        .then(function(blob) {
          var url = window.URL.createObjectURL(blob);
          var a = document.createElement('a');
          a.href = url;
          a.download = 'audio_' + date + '_dialog.wav';
          a.click();
          window.URL.revokeObjectURL(url);
        })
        .catch(function(err) {
          alert('Ошибка скачивания: ' + err.message);
        });
      });
    }
  }

  function getDialogTypeLabel(type) {
    var labels = {
      "help": "Помощь",
      "service": "Сервис",
      "complaint": "Рекламация",
      "buy": "Покупка",
      "other": "Другое",
      "unknown": "Неизвестно"
    };
    return labels[type] || (type || "-");
  }

  var userComments = {};

  function loadComments() {
    fetch('/api/comments')
      .then(function(r) { return r.json(); })
      .then(function(data) {
        userComments = data || {};
      })
      .catch(function() {
        userComments = {};
      });
  }

  function saveCommentInput(input) {
    var commentKey = input.dataset.commentKey;
    var comment = input.value.trim();
    
    if (comment && commentKey) {
      userComments[commentKey] = comment;
      input.style.borderColor = '#10b981';
      input.style.backgroundColor = '#ecfdf5';
      fetch('/api/comments', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({key: commentKey, comment: comment})
      }).then(function() {
        setTimeout(function() {
          input.style.borderColor = '#e0e7ff';
          input.style.backgroundColor = '#fafbff';
        }, 1500);
      }).catch(function() {});
    }
  }

  var modalOverlay = null;
  var modalError = null;
  var manualForm = null;

  function initModal() {
    modalOverlay = document.getElementById('modal-overlay');
    modalError = document.getElementById('modal-error');
    manualForm = document.getElementById('manual-dialog-form');
    
    document.getElementById('modal-cancel').addEventListener('click', closeModal);
    modalOverlay.addEventListener('click', function(e) {
      if (e.target === modalOverlay) {
        closeModal();
      }
    });
    
    manualForm.addEventListener('submit', function(e) {
      e.preventDefault();
      saveManualDialog();
    });
  }

  function openAddDialogModal() {
    if (!modalOverlay) {
      initModal();
    }
    modalError.classList.remove('active');
    modalError.textContent = '';
    manualForm.reset();
    modalOverlay.classList.add('active');
  }

  function closeModal() {
    if (modalOverlay) {
      modalOverlay.classList.remove('active');
    }
  }

  function saveManualDialog() {
    var dateStr = dateSelect.value;
    var storeId = storeSelect.value;
    
    if (!dateStr || !storeId || storeId === "all") {
      showModalError('Выберите дату и магазин');
      return;
    }
    
    var startTimeInput = document.getElementById('manual-start-time').value;
    var endTimeInput = document.getElementById('manual-end-time').value;
    var dialogType = document.getElementById('manual-dialog-type').value.trim();
    var result = document.getElementById('manual-result').value.trim();
    
    if (!startTimeInput || !endTimeInput || !dialogType || !result) {
      showModalError('Все поля обязательны');
      return;
    }
    
    // Parse time strings (HH:MM:SS or HH:MM)
    var startParts = startTimeInput.split(':');
    var endParts = endTimeInput.split(':');
    
    var startMinutes = parseInt(startParts[0]) * 60 + parseInt(startParts[1]) + (startParts[2] ? parseInt(startParts[2]) / 60 : 0);
    var endMinutes = parseInt(endParts[0]) * 60 + parseInt(endParts[1]) + (endParts[2] ? parseInt(endParts[2]) / 60 : 0);
    
    if (startMinutes >= endMinutes) {
      showModalError('Время начала должно быть раньше времени окончания');
      return;
    }
    
    var startTime = startTimeInput;
    var endTime = endTimeInput;
    
    fetch('/api/manual-dialogs', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        date: dateStr,
        store_id: storeId,
        start_time: startTime,
        end_time: endTime,
        dialog_type: dialogType,
        result: result
      })
    }).then(function(response) {
      return response.json().then(function(data) {
        return {response: response, data: data};
      });
    }).then(function(res) {
      if (res.response.ok) {
        closeModal();
        loadReportData();
      } else {
        showModalError(res.data.detail || 'Ошибка при сохранении');
      }
    }).catch(function(err) {
      showModalError('Ошибка: ' + err.message);
    });
  }

  function showModalError(message) {
    modalError.textContent = message;
    modalError.classList.add('active');
  }

  function deleteManualDialog(dialogId) {
    if (!confirm('Удалить этот ручной диалог?')) {
      return;
    }
    
    fetch('/api/manual-dialogs/' + encodeURIComponent(dialogId), {
      method: 'DELETE'
    }).then(function(response) {
      return response.json();
    }).then(function(data) {
      if (data.status === 'ok') {
        loadReportData();
      } else {
        alert('Ошибка при удалении: ' + (data.detail || 'Неизвестная ошибка'));
      }
    }).catch(function(err) {
      alert('Ошибка: ' + err.message);
    });
  }

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  function loadStores() {
    return fetch("/api/stores")
      .then(function(response) {
        if (!response.ok) {
          throw new Error("Failed to load stores");
        }
        return response.json();
      })
      .then(function(data) {
        storeSelect.innerHTML = '<option value="all">Все магазины</option>';
        for (var i = 0; i < data.stores.length; i++) {
          var option = document.createElement("option");
          option.value = data.stores[i];
          option.textContent = data.stores[i];
          storeSelect.appendChild(option);
        }
      })
      .catch(function(error) {
        showError("Не удалось загрузить список магазинов: " + error.message);
      });
  }

  function loadDates() {
    return fetch("/api/reports/dates")
      .then(function(response) {
        if (!response.ok) {
          throw new Error("Failed to load dates");
        }
        return response.json();
      })
      .then(function(data) {
        dateSelect.innerHTML = "";
        for (var i = 0; i < data.dates.length; i++) {
          var option = document.createElement("option");
          option.value = data.dates[i];
          option.textContent = data.dates[i].split("-").reverse().join(".");
          dateSelect.appendChild(option);
        }
      })
      .catch(function(error) {
        showError("Не удалось загрузить список дат: " + error.message);
      });
  }

  async function loadReportData() {
    var dateStr = dateSelect.value;
    if (!dateStr) return;

    hideError();
    tableContainer.innerHTML = '<div class="loading"><div class="spinner"></div><div>Загрузка данных...</div></div>';

    try {
      var storeFilter = storeSelect.value === "all" ? undefined : storeSelect.value;

      var url = "/api/reports/" + encodeURIComponent(dateStr) + "/data";
      if (storeFilter) {
        url += "?store_filter=" + encodeURIComponent(storeFilter);
      }

      var response = await fetch(url);
      if (!response.ok) {
        throw new Error("Failed to load report data");
      }

      var data = await response.json();
      currentData = data;
      renderTable(data.dialogs);
    } catch (error) {
      showError("Не удалось загрузить данные: " + error.message);
    }
  }

  dateSelect.addEventListener("change", function () {
    localStorage.setItem("audio_date", dateSelect.value);
    loadReportData();
  });

  storeSelect.addEventListener("change", function () {
    localStorage.setItem("audio_store", storeSelect.value);
    loadReportData();
  });

  async function initPage() {
    await loadStores();
    await loadDates();
    loadComments();
    initModal();
    
    // Restore saved date and store after loading options
    var savedDate = localStorage.getItem("audio_date");
    var savedStore = localStorage.getItem("audio_store");
    if (savedDate && dateSelect.querySelector("[value='" + savedDate + "']")) {
      dateSelect.value = savedDate;
    }
    if (savedStore && storeSelect.querySelector("[value='" + savedStore + "']")) {
      storeSelect.value = savedStore;
    }
    
    // Load report data if date is selected
    if (dateSelect.value) {
      loadReportData();
    }
    
    // Make deleteManualDialog globally accessible
    window.deleteManualDialog = deleteManualDialog;
  }
  
  initPage();
})();
</script>
</body>
</html>
"""

AUDIO_TIME_RANGE_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Скачать аудио по времени — Audio Analytics</title>
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
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
      "Helvetica Neue", Arial, sans-serif;
    padding: 24px;
  }
  .container {
    max-width: 600px;
    margin: 0 auto;
  }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    box-shadow: 0 1px 3px rgba(16, 24, 40, 0.06), 0 8px 24px rgba(16, 24, 40, 0.06);
    padding: 32px;
  }
  h1 {
    margin: 0 0 8px;
    font-size: 24px;
    font-weight: 650;
    letter-spacing: -0.02em;
  }
  .subtitle {
    margin: 0 0 24px;
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
  select, input[type="datetime-local"] {
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
  select:focus, input[type="datetime-local"]:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.15);
  }
  button {
    width: 100%;
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
    padding: 12px 14px;
    font-size: 14px;
    line-height: 1.45;
    color: var(--error-text);
    background: var(--error-bg);
    border: 1px solid var(--error-border);
    border-radius: 10px;
    margin-top: 16px;
  }
  .alert.visible { display: block; }
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
  <div class="container">
    <div class="card">
      <h1>Скачать аудио по времени</h1>
      <p class="subtitle">Укажите магазин и временной интервал для выгрузки аудио.</p>

      <form id="audio-form">
        <label for="store-select">Магазин</label>
        <select id="store-select" required></select>

        <label for="start_time">Время начала</label>
        <input type="datetime-local" id="start_time" name="start_time" required>

        <label for="end_time">Время окончания</label>
        <input type="datetime-local" id="end_time" name="end_time" required>

        <button type="submit" id="submit-btn">
          <span class="spinner" id="spinner" hidden></span>
          <span id="btn-label">Скачать аудио</span>
        </button>

        <div class="alert" id="alert" role="alert"></div>
      </form>

      <a href="/audio" class="nav-link">Скачать аудио по отчётам</a>
      <a href="/" class="nav-link">← Вернуться к отчётам</a>
    </div>
  </div>

<script>
(function () {
  var form = document.getElementById("audio-form");
  var storeSelect = document.getElementById("store-select");
  var startTimeInput = document.getElementById("start_time");
  var endTimeInput = document.getElementById("end_time");
  var button = document.getElementById("submit-btn");
  var label = document.getElementById("btn-label");
  var spinner = document.getElementById("spinner");
  var alertBox = document.getElementById("alert");
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
  }

  function validateForm() {
    var store = storeSelect.value;
    var startValue = startTimeInput.value;
    var endValue = endTimeInput.value;

    if (!store) {
      showError("Выберите магазин.");
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

  async function loadStores() {
    try {
      var response = await fetch("/api/stores");
      if (!response.ok) {
        throw new Error("Failed to load stores");
      }
      var data = await response.json();
      for (var i = 0; i < data.stores.length; i++) {
        var option = document.createElement("option");
        option.value = data.stores[i];
        option.textContent = data.stores[i];
        storeSelect.appendChild(option);
      }
    } catch (error) {
      showError("Не удалось загрузить список магазинов: " + error.message);
    }
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
    setLoading(true);

    try {
      var store = storeSelect.value;
      var startTimeIso = toIsoLocal(startTimeInput.value);
      var endTimeIso = toIsoLocal(endTimeInput.value);

      var url = "/audio/download?store=" + encodeURIComponent(store)
              + "&start_time=" + encodeURIComponent(startTimeIso)
              + "&end_time=" + encodeURIComponent(endTimeIso);

      var response = await fetch(url);

      if (!response.ok) {
        var detail = "Ошибка при загрузке аудио";
        try {
          var errBody = await response.json();
          if (errBody && errBody.detail) {
            detail = errBody.detail;
          }
        } catch (e) {}
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
    } catch (error) {
      showError(error.message || "Не удалось загрузить аудио. Попробуйте ещё раз.");
    } finally {
      setLoading(false);
    }
  });

  loadStores();
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


@app.get("/audio/time-range", response_class=HTMLResponse)
def audio_time_range_page():
    return AUDIO_TIME_RANGE_HTML


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


@app.get("/api/stores")
def get_stores():
    """Get fixed list of all known stores"""
    try:
        stores = get_all_stores()
        return {"stores": stores}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get stores: {str(e)}")


@app.get("/api/comments")
def get_comments():
    """Get all user comments"""
    try:
        if COMMENTS_FILE.exists():
            with open(COMMENTS_FILE, "r", encoding="utf-8") as f:
                comments = json.load(f)
        else:
            comments = {}
        return comments
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get comments: {str(e)}")


@app.post("/api/comments")
def save_comment(comment_data: dict):
    """Save a user comment"""
    try:
        key = comment_data.get("key")
        comment = comment_data.get("comment")
        if not key or not comment:
            raise HTTPException(status_code=400, detail="Missing key or comment")
        
        if COMMENTS_FILE.exists():
            with open(COMMENTS_FILE, "r", encoding="utf-8") as f:
                comments = json.load(f)
        else:
            comments = {}
        
        comments[key] = comment
        with open(COMMENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(comments, f, ensure_ascii=False, indent=2)
        
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save comment: {str(e)}")


@app.get("/api/comments/export-gt")
def export_comments_to_gt():
    """Export comments to GT format for training"""
    try:
        if not COMMENTS_FILE.exists():
            raise HTTPException(status_code=404, detail="No comments found")
        
        with open(COMMENTS_FILE, "r", encoding="utf-8") as f:
            comments = json.load(f)
        
        if not comments:
            raise HTTPException(status_code=404, detail="No comments to export")
        
        rows = []
        for key, comment in comments.items():
            parts = key.split("_")
            if len(parts) >= 3:
                date_str = parts[0]
                store_id = parts[1]
                client_id = "_".join(parts[2:])
                rows.append({
                    "date": date_str,
                    "store_id": store_id,
                    "client_id": client_id,
                    "user_comment": comment,
                })
        
        df = pd.DataFrame(rows)
        output_file = REPORT_DIR / "comments_gt_export.xlsx"
        df.to_excel(output_file, index=False)
        
        return FileResponse(
            output_file,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="comments_gt_export.xlsx"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to export comments: {str(e)}")


def load_manual_dialogs():
    """Load manual dialogs from JSON file"""
    if MANUAL_DIALOGS_FILE.exists():
        with open(MANUAL_DIALOGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_manual_dialogs(dialogs):
    """Save manual dialogs to JSON file"""
    MANUAL_DIALOGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(MANUAL_DIALOGS_FILE, "w", encoding="utf-8") as f:
        json.dump(dialogs, f, ensure_ascii=False, indent=2)


def parse_time_to_minutes(time_str):
    """Parse time string (HH:MM or HH:MM:SS) to minutes from midnight"""
    parts = time_str.split(':')
    hours = int(parts[0])
    minutes = int(parts[1])
    seconds = int(parts[2]) if len(parts) > 2 else 0
    return hours * 60 + minutes + seconds / 60

def check_time_overlap(start_time_str, end_time_str, existing_dialogs, date_str, store_id):
    """
    Check if new dialog time overlaps with existing dialogs.
    Returns (is_valid, error_message)
    """
    try:
        new_start = parse_time_to_minutes(start_time_str)
        new_end = parse_time_to_minutes(end_time_str)
    except (ValueError, IndexError):
        return False, "Неверный формат времени. Используйте HH:MM или HH:MM:SS"
    
    if new_start >= new_end:
        return False, "Время начала должно быть раньше времени окончания"
    
    for dialog in existing_dialogs:
        if dialog.get("date") != date_str or dialog.get("store_id") != store_id:
            continue
        
        try:
            existing_start = parse_time_to_minutes(dialog["start_time"])
            existing_end = parse_time_to_minutes(dialog["end_time"])
        except (ValueError, KeyError, IndexError):
            continue
        
        if new_start < existing_end and new_end > existing_start:
            return False, f"Интервал пересекается с существующей сессией: {dialog['start_time']} — {dialog['end_time']}"
    
    return True, None


@app.get("/api/manual-dialogs")
def get_manual_dialogs(
    date: str = Query(None, description="Filter by date (YYYY-MM-DD)"),
    store: str = Query(None, description="Filter by store"),
):
    """Get manual dialogs with optional filters"""
    try:
        dialogs = load_manual_dialogs()
        
        if date:
            dialogs = [d for d in dialogs if d.get("date") == date]
        if store:
            dialogs = [d for d in dialogs if d.get("store_id") == store]
        
        return {"dialogs": dialogs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get manual dialogs: {str(e)}")


@app.post("/api/manual-dialogs")
def create_manual_dialog(dialog_data: dict):
    """Create a new manual dialog"""
    try:
        required_fields = ["date", "store_id", "start_time", "end_time", "dialog_type", "result"]
        for field in required_fields:
            if field not in dialog_data or not dialog_data[field]:
                raise HTTPException(status_code=400, detail=f"Обязательное поле отсутствует: {field}")
        
        date_str = dialog_data["date"]
        store_id = dialog_data["store_id"]
        start_time_input = dialog_data["start_time"]
        end_time_input = dialog_data["end_time"]
        
        # Normalize time format to HH:MM:SS
        def normalize_time(time_str):
            parts = time_str.split(':')
            if len(parts) == 2:
                return time_str + ':00'
            return time_str
        
        start_time = normalize_time(start_time_input)
        end_time = normalize_time(end_time_input)
        
        existing_dialogs = load_manual_dialogs()
        
        is_valid, error_msg = check_time_overlap(start_time, end_time, existing_dialogs, date_str, store_id)
        if not is_valid:
            raise HTTPException(status_code=400, detail=error_msg)
        
        import uuid
        new_dialog = {
            "id": str(uuid.uuid4()),
            "date": date_str,
            "store_id": store_id,
            "start_time": start_time,
            "end_time": end_time,
            "dialog_type": dialog_data["dialog_type"],
            "result": dialog_data["result"],
            "is_manual": True,
            "created_at": datetime.now().isoformat(),
        }
        
        existing_dialogs.append(new_dialog)
        save_manual_dialogs(existing_dialogs)
        
        return {"status": "ok", "dialog": new_dialog}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create manual dialog: {str(e)}")


@app.delete("/api/manual-dialogs/{dialog_id}")
def delete_manual_dialog(dialog_id: str):
    """Delete a manual dialog"""
    try:
        dialogs = load_manual_dialogs()
        dialogs = [d for d in dialogs if d.get("id") != dialog_id]
        save_manual_dialogs(dialogs)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete manual dialog: {str(e)}")


@app.get("/api/reports/dates")
def get_reports_dates():
    """Get list of available report dates"""
    try:
        dates = get_available_dates()
        return {"dates": dates}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get report dates: {str(e)}")


@app.get("/api/reports/{date_str}/data")
def get_report_data(
    date_str: str,
    store_filter: Optional[str] = Query(None, description="Filter by store (optional)"),
    client_filter: Optional[str] = Query(None, description="Filter by client (optional)"),
):
    """Get report data for a given date with optional filters"""
    # Validate date format
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    df = load_report_data(date_str)
    if df is None:
        raise HTTPException(status_code=404, detail=f"No report found for date {date_str}")

    # Apply filters
    if store_filter and store_filter != "all":
        df = df[df["store_id"] == store_filter]
    if client_filter and client_filter != "all":
        df = df[df["client_id"] == client_filter]

    if df.empty:
        return {
            "date": date_str,
            "dialogs": [],
            "stores": [],
            "clients": [],
        }

    # Prepare dialog data
    dialogs = []
    for _, row in df.iterrows():
        start_time = row["dialog_start_at"]
        end_time = row["dialog_end_at"]
        session_ids_str = row["session_ids"] if pd.notna(row["session_ids"]) else ""
        session_ids = [s.strip() for s in session_ids_str.split(",") if s.strip()]

        dialogs.append({
            "client_id": row["client_id"],
            "store_id": row["store_id"],
            "seller_id": row["seller_id"],
            "start_time": start_time.strftime("%H:%M:%S") if hasattr(start_time, "strftime") else str(start_time),
            "end_time": end_time.strftime("%H:%M:%S") if hasattr(end_time, "strftime") else str(end_time),
            "duration_sec": row["dialog_duration_sec"],
            "session_ids": session_ids,
            "is_sale": row["is_sale"],
            "dialog_type": row["dialog_type"],
            "model_reasoning": row["model_reasoning"] if pd.notna(row["model_reasoning"]) else "",
            "recognition_text": row["recognition_text"] if pd.notna(row["recognition_text"]) else "",
        })

    # Get unique stores and clients from filtered data
    stores = df["store_id"].unique().tolist() if "store_id" in df.columns else []
    clients = df["client_id"].unique().tolist() if "client_id" in df.columns else []

    # Add manual dialogs for this date and store
    manual_dialogs = load_manual_dialogs()
    for md in manual_dialogs:
        if md.get("date") == date_str:
            if not store_filter or md.get("store_id") == store_filter:
                dialogs.append({
                    "client_id": "Manual",
                    "store_id": md.get("store_id"),
                    "seller_id": "",
                    "start_time": md.get("start_time"),
                    "end_time": md.get("end_time"),
                    "duration_sec": 0,
                    "session_ids": [],
                    "is_sale": md.get("result") == "Покупка",
                    "dialog_type": md.get("dialog_type"),
                    "model_reasoning": "Ручной диалог",
                    "recognition_text": "",
                    "is_manual": True,
                    "manual_id": md.get("id"),
                    "result": md.get("result"),
                })

    # Sort all dialogs by start_time
    dialogs.sort(key=lambda x: x.get("start_time", ""))

    return {
        "date": date_str,
        "dialogs": dialogs,
        "stores": sorted(stores),
        "clients": sorted(clients),
    }


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


@app.post("/api/audio/download-by-dialog")
def download_audio_by_dialog(
    date_str: str = Form(..., description="Date in YYYY-MM-DD format"),
    session_ids: str = Form(..., description="Comma-separated session IDs"),
):
    """Download audio for a specific dialog by session IDs"""
    # Parse session IDs
    session_list = [s.strip() for s in session_ids.split(",") if s.strip()]
    if not session_list:
        raise HTTPException(status_code=400, detail="No session IDs provided")

    # Extract date from first session ID (format: YYYYMMDD-HHMMSS-...)
    try:
        first_session = session_list[0]
        session_date = first_session.split("-")[0]
        session_date_str = f"{session_date[:4]}-{session_date[4:6]}-{session_date[6:8]}"
    except (IndexError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid session ID format")

    s3_client = get_s3_client()

    # Download and merge audio for all sessions
    all_audio_data = []
    for session_id in session_list:
        try:
            audio_data = download_session_chunks(s3_client, session_id)
            if audio_data:
                all_audio_data.append(audio_data)
        except Exception:
            continue

    if not all_audio_data:
        raise HTTPException(
            status_code=404,
            detail="No audio data available for the selected sessions",
        )

    audio_buffer = merge_audio_sessions(all_audio_data)
    audio_buffer.seek(0)

    # Generate filename
    safe_date = date_str.replace("-", "")
    filename = f"audio_{safe_date}_dialog.wav"

    return Response(
        content=audio_buffer.read(),
        media_type="audio/wav",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )