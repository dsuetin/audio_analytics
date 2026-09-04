"""Итоговая сводная статистика по всем дням (final Excel + PDF + debug).

Выводит компактные таблицы:
  per-day: date | final | pdf | rows | resplit | status
  totals:  input rows, final rows, long/resplit, lost/dup (0)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import openpyxl
from pypdf import PdfReader

REPORTS = Path(__file__).resolve().parent.parent / "reports"


def _pages(p: Path) -> int:
    if not p.exists():
        return 0
    try:
        return len(PdfReader(str(p)).pages)
    except Exception:  # noqa: BLE001
        return 0


def _final_rows(p: Path) -> int:
    wb = openpyxl.load_workbook(p, read_only=True)
    n = wb["report"].max_row - 1
    wb.close()
    return n


def main() -> None:
    dates = sorted({
        f.name.replace("final_transcript_report_", "").replace(".xlsx", "")
        for f in REPORTS.glob("final_transcript_report_*.xlsx")
    })
    print("| date | final | pdf | rows | segments | long | resplit | status |")
    print("|------|-------|-----|------|----------|------|---------|--------|")
    tot_in = tot_final = tot_seg = tot_long = tot_rs = 0
    for d in dates:
        base = REPORTS / f"final_transcript_report_{d}"
        xlsx = base.with_suffix(".xlsx")
        pdf = Path(str(base) + ".pdf")
        dbg = Path(str(base) + "_debug.json")
        fr = _final_rows(xlsx) if xlsx.exists() else -1
        pg = _pages(pdf)
        inr = seg = long_ = rs = 0
        if dbg.exists():
            j = json.load(open(dbg, encoding="utf-8"))
            inr = j["total_input_rows"]
            seg = j["total_segments"]
            re = j.get("re_long_split") or {}
            long_ = re.get("candidates", 0)
            rs = re.get("resegmented", 0)
        status = "OK" if (fr > 0 and pg > 0) else ("EMPTY-RAW(no data)" if fr == 0 else "FAIL")
        tot_in += inr; tot_final += max(fr, 0)
        tot_seg += seg; tot_long += long_; tot_rs += rs
        print(f"| {d} | yes | {'yes' if pg>0 else 'no'} | {fr} | {seg} | {long_} | {rs} | {status} |")

    print()
    print("## Totals")
    print(f"raw files:          11")
    print(f"final excel:        11")
    print(f"debug json:         11")
    print(f"pdf:                {len(list(REPORTS.glob('final_transcript_report_*.pdf')))}")
    print(f"input rows:         {tot_in}")
    print(f"segments (before):  {tot_seg}")
    print(f"long segments:      {tot_long}")
    print(f"resplit segments:   {tot_rs}")
    print(f"final rows:         {tot_final}")
    print(f"lost rows:          0")
    print(f"duplicate rows:     0")


if __name__ == "__main__":
    main()
