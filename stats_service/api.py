from datetime import date
from pathlib import Path
import subprocess
import sys
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from stats_service.create_final_report import create_final_report

app = FastAPI()

REPORT_DIR = Path(
    os.getenv(
        "REPORT_OUTPUT_DIR",
        "/reports",
    )
)


@app.get("/reports/daily")
def generate_report(report_date: date):

    REPORT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    result = subprocess.run(
        [
            sys.executable,
            "stats_service/scheduler.py",
            "--once",
            "--date",
            report_date.isoformat(),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=result.stderr or result.stdout,
        )

    prefix = f"transcript_report_{report_date.isoformat()}"

    xlsx_path = REPORT_DIR / f"{prefix}.xlsx"
    pdf_path = REPORT_DIR / f"{prefix}.pdf"

    for path in (xlsx_path, pdf_path):
        if not path.exists():
            raise HTTPException(
                status_code=500,
                detail=f"Report file not found: {path.name}",
            )
        
    final_xlsx_path = REPORT_DIR / f"final_{xlsx_path.name}"

    create_final_report(
        str(xlsx_path),
        str(final_xlsx_path),
    )

    return FileResponse(
        path=final_xlsx_path,
        filename=final_xlsx_path.name,
        media_type="application/zip",
    )