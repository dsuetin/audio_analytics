from datetime import date
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import subprocess
import sys
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

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

    zip_path = REPORT_DIR / f"{prefix}.zip"

    with ZipFile(
        zip_path,
        "w",
        compression=ZIP_DEFLATED,
    ) as archive:
        archive.write(xlsx_path, arcname=xlsx_path.name)
        archive.write(pdf_path, arcname=pdf_path.name)

    return FileResponse(
        path=zip_path,
        filename=zip_path.name,
        media_type="application/zip",
    )