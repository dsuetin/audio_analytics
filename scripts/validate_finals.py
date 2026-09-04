"""Валидация final Excel-отчётов (reports/final_transcript_report_<DATE>.xlsx).

Проверяет:
  - открывается;
  - есть sheet `report`;
  - необходимые колонки;
  - client_id валидный (не содержит `offline_...`);
  - dialog_type не пуст у customer rows;
  - model_reasoning присутствует;
  - нет `offline_...` в client_id.

НЕ запускает LLM. Вывод компактный.
"""
from __future__ import annotations

import sys
from pathlib import Path

import openpyxl

REQUIRED_COLUMNS = (
    "store_id", "seller_id", "client_id", "recognition_text", "dialog_type",
    "is_sale", "loss_reason", "is_alarm_triggered", "dialog_start_at",
    "dialog_end_at", "dialog_duration_sec", "session_ids", "model_reasoning",
)

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


def validate_one(date: str) -> tuple[bool, list[str]]:
    """Валидирует один final Excel. Возвращает (ok, issues)."""
    issues: list[str] = []
    path = REPORTS_DIR / f"final_transcript_report_{date}.xlsx"
    if not path.exists():
        return False, ["missing file"]

    try:
        wb = openpyxl.load_workbook(path, read_only=True)
    except Exception as exc:  # noqa: BLE001
        return False, [f"open failed: {exc!r}"]

    try:
        if "report" not in wb.sheetnames:
            return False, [f"no 'report' sheet; got {wb.sheetnames}"]
        ws = wb["report"]
        rows = ws.iter_rows(values_only=True)
        try:
            header = next(rows)
        except StopIteration:
            return False, ["empty sheet (no header)"]

        header = [str(h) if h is not None else "" for h in header]
        missing_cols = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing_cols:
            issues.append(f"missing columns: {missing_cols}")

        idx = {name: header.index(name) for name in header if name in REQUIRED_COLUMNS}
        n = 0
        blank_client = 0
        offline_client = 0
        blank_dialog_type = 0
        blank_reasoning = 0
        for row in rows:
            if row is None or all(c is None for c in row):
                continue
            n += 1

            def col(name):
                i = idx.get(name)
                if i is None or i >= len(row):
                    return None
                return row[i]

            if "client_id" in idx:
                cid = col("client_id")
                if cid is None or str(cid).strip() == "":
                    blank_client += 1
                elif "offline_" in str(cid):
                    offline_client += 1
            if "dialog_type" in idx:
                dt = col("dialog_type")
                if dt is None or str(dt).strip() == "":
                    blank_dialog_type += 1
            if "model_reasoning" in idx:
                mr = col("model_reasoning")
                if mr is None or str(mr).strip() == "":
                    blank_reasoning += 1
    finally:
        wb.close()

    # строка с 0 — допустимо (тестовый raw без данных)
    if n == 0:
        if missing_cols:
            return False, issues
        # пустой финал — считаем валидным (нет данных для проверки), но помечаем
        return True, ["0 rows (empty day — raw had no data)"] if not missing_cols else issues
    if blank_client:
        issues.append(f"{blank_client} rows with blank client_id")
    if offline_client:
        issues.append(f"{offline_client} rows with 'offline_...' client_id")
    if blank_dialog_type:
        issues.append(f"{blank_dialog_type} rows with blank dialog_type")
    if blank_reasoning:
        issues.append(f"{blank_reasoning} rows with blank model_reasoning")

    if missing_cols:
        return False, issues
    # client_id пуст / offline — это ошибки; dialog_type/reasoning — предупреждения (но тоже ошибка по спецификации)
    return (not (blank_client or offline_client or blank_dialog_type or blank_reasoning)), issues


def main() -> int:
    dates = sorted({
        p.name.replace("final_transcript_report_", "").replace(".xlsx", "")
        for p in REPORTS_DIR.glob("final_transcript_report_*.xlsx")
    })
    all_ok = True
    for d in dates:
        ok, issues = validate_one(d)
        # пересчёт количества строк для отчёта
        if not ok:
            all_ok = False
        print(f"{d} {'OK ' if ok else 'FAIL'} | {len(issues)} issue(s)" + ("" if not issues else " | " + "; ".join(issues)))
    print()
    print("ALL FINAL VALID" if all_ok else "SOME FINAL INVALID")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
