from datetime import date
from pathlib import Path
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

app = FastAPI(title="Stats Service")

REPORT_DIR = Path(
    os.getenv(
        "REPORT_OUTPUT_DIR",
        "/reports",
    )
)


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


@app.get("/", response_class=HTMLResponse)
def index_page():
    return INDEX_HTML


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