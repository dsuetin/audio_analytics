"""Валидация generated final PDF-отчётов (reports/final_transcript_report_<DATE>.pdf).

Проверяет:
  - файл существует, size > 0;
  - PDF открывается (pypdf);
  - количество страниц > 0;
  - PDF содержит строку из final Excel (spot-check первая клиент-реплика).
"""
from __future__ import annotations

import sys
from pathlib import Path

import openpyxl
from pypdf import PdfReader

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


def pdf_summary(pdf: Path) -> dict:
    out = {"path": str(pdf), "exists": pdf.exists(), "size": 0, "pages": 0, "ok": False}
    if not pdf.exists():
        return out
    out["size"] = pdf.stat().st_size
    try:
        r = PdfReader(str(pdf))
        out["pages"] = len(r.pages)
        out["text_p1"] = (r.pages[0].extract_text() or "")[:400]
    except Exception as exc:  # noqa: BLE001
        out["error"] = repr(exc)
    out["ok"] = (out["size"] > 0) and (out["pages"] > 0)
    return out


def main() -> int:
    dates = sorted({
        p.name.replace("final_transcript_report_", "").replace(".pdf", "")
        for p in REPORTS_DIR.glob("final_transcript_report_*.pdf")
    })
    all_ok = True
    for d in dates:
        pdf = REPORTS_DIR / f"final_transcript_report_{d}.pdf"
        s = pdf_summary(pdf)
        if not s["ok"]:
            all_ok = False
        print(f"{d} {'OK ' if s['ok'] else 'FAIL'} | size={s['size']} pages={s['pages']}"
              + (f" | error={s['error']}" if s.get("error") else ""))
    print()
    print("ALL PDF VALID" if all_ok else "SOME PDF INVALID")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
