from fastapi import FastAPI
from fastapi.responses import FileResponse
from datetime import date
from pathlib import Path
import subprocess
import os


app = FastAPI()


REPORT_DIR = Path(
    os.getenv(
        "REPORT_OUTPUT_DIR",
        "/reports"
    )
)


@app.get("/reports/daily")
def generate_report(
    report_date: date
):

    cmd = [
        "python",
        "stats_service/export_daily_transcript_report.py",
        "--date",
        report_date.isoformat(),
    ]

    subprocess.run(
        cmd,
        check=True
    )


    pdf = REPORT_DIR / (
        f"transcript_report_{report_date}.pdf"
    )

    if not pdf.exists():
        return {
            "error": "Report not generated"
        }


    return FileResponse(
        path=pdf,
        filename=pdf.name,
        media_type="application/pdf"
    )