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

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from stats_service.stores import get_all_stores

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

      <a href="/" class="nav-link">← Вернуться к отчётам</a>
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
    var html = '<table><thead><tr>' +
      '<th>Магазин</th>' +
      '<th>Клиент</th>' +
      '<th>Начало</th>' +
      '<th>Конец</th>' +
      '<th>Длительность</th>' +
      '<th>Аудио</th>' +
      '</tr></thead><tbody>';

    for (var i = 0; i < dialogs.length; i++) {
      var d = dialogs[i];
      var sessionIdsEncoded = encodeURIComponent(d.session_ids.join(","));
      var downloadUrl = "/api/audio/download-by-dialog?date_str=" + encodeURIComponent(dateStr) + "&session_ids=" + sessionIdsEncoded;
      html += '<tr>' +
        '<td>' + escapeHtml(d.store_id) + '</td>' +
        '<td>' + escapeHtml(d.client_id) + '</td>' +
        '<td>' + d.start_time + '</td>' +
        '<td>' + d.end_time + '</td>' +
        '<td>' + formatDuration(d.duration_sec) + '</td>' +
        '<td><a href="' + downloadUrl + '" class="btn">Скачать</a></td>' +
        '</tr>';
    }

    html += '</tbody></table>';
    tableContainer.innerHTML = html;
  }

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  async function loadStores() {
    try {
      var response = await fetch("/api/stores");
      if (!response.ok) {
        throw new Error("Failed to load stores");
      }
      var data = await response.json();
      storeSelect.innerHTML = '<option value="all">Все магазины</option>';
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

  async function loadDates() {
    try {
      var response = await fetch("/api/reports/dates");
      if (!response.ok) {
        throw new Error("Failed to load dates");
      }
      var data = await response.json();
      dateSelect.innerHTML = "";
      for (var i = 0; i < data.dates.length; i++) {
        var option = document.createElement("option");
        option.value = data.dates[i];
        option.textContent = data.dates[i].split("-").reverse().join(".");
        dateSelect.appendChild(option);
      }
    } catch (error) {
      showError("Не удалось загрузить список дат: " + error.message);
    }
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
    loadReportData();
  });

  storeSelect.addEventListener("change", loadReportData);

  loadStores();
  loadDates();
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


@app.get("/api/stores")
def get_stores():
    """Get fixed list of all known stores"""
    try:
        stores = get_all_stores()
        return {"stores": stores}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get stores: {str(e)}")


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
        })

    # Get unique stores and clients from filtered data
    stores = df["store_id"].unique().tolist() if "store_id" in df.columns else []
    clients = df["client_id"].unique().tolist() if "client_id" in df.columns else []

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


@app.get("/api/audio/download-by-dialog")
def download_audio_by_dialog(
    date_str: str = Query(..., description="Date in YYYY-MM-DD format"),
    session_ids: str = Query(..., description="Comma-separated session IDs"),
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