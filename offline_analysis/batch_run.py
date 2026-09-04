"""Batch-пересчёт ВСЕХ raw transcript reports новой версией offline-конвейера.

Один helper-скрипт:
  * НЕ дублирует логику offline-analysis — просто вызывает существующий ``run.run(args)``
    (сегментация -> re-segmentation длинных dialog -> LLM classification -> Excel + debug).
  * ОБЫЧНЫЙ режим: re-segmentation ENABLED (``no_resplit=False``) — ``--no-resplit`` НЕ используется.
  * Безопасная замена: результат кладётся в temporary-каталог, затем — ТОЛЬКО если валиден
    — атомарно переносится в ``reports/final_transcript_report_<DATE>.xlsx``
    (``os.replace`` = атомарная операция на POSIX). Старый final остаётся на месте
    до успешного валидированного replacement.
  * Ошибка одного файла НЕ останавливает batch — список ``failed`` печатается в конце.
  * Файлы обрабатываются ПОСЛЕДОВАТЕЛЬНО (``OFFLINE_WORKERS=1``, ``run.args.workers=1``);
    один день за раз (не десятки параллельных LLM-запросов).
  * RAW-файлы (``transcript_report_*.xlsx``) НЕ модифицируются, ни один из них не трогается.

Запуск:
    OFFLINE_LLM_TIMEOUT=3600 OFFLINE_WORKERS=1 \
    python3 offline_analysis/batch_run.py
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import types
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook


_REPO = Path(__file__).resolve().parents[1]
_REPORTS = _REPO / "reports"
sys.path.insert(0, str(_REPO))

# environment: один worker, длинный timeout (если ещё не заданы внешним сценарием).
os.environ.setdefault("OFFLINE_WORKERS", "1")
os.environ.setdefault("OFFLINE_LLM_TIMEOUT", "3600")
os.environ.setdefault("OFFLINE_NUM_CTX", "131072")
os.environ.setdefault("OFFLINE_NUM_PREDICT", "128000")

# Импортируем только после того, как ``sys.path`` содержит корень репо.
from offline_analysis import run as offline_run  # noqa: E402
from offline_analysis.segmentation import load_raw, df_to_rows  # noqa: E402
from offline_analysis.taxonomy import CONFIDENCE_MIN  # noqa: E402


# ---------------------------------------------------------------------------
# Поиск raw-файлов
# ---------------------------------------------------------------------------

RAW_RE = re.compile(r"^transcript_report_(\d{4}-\d{2}-\d{2})\.xlsx$")


def find_raw_files(reports_dir: Path) -> list[tuple[str, Path]]:
    """Возвращает ``[(date, path), ...]`` отсортировано по дате возрастанию.

    Берет ТОЛЬКО ``transcript_report_YYYY-MM-DD.xlsx`` — НЕ ``final_*`` и не другие .xlsx.
    """
    out: list[tuple[str, Path]] = []
    if not reports_dir.exists():
        return out
    for p in reports_dir.iterdir():
        m = RAW_RE.match(p.name)
        if m and p.is_file():
            out.append((m.group(1), p))
    out.sort(key=lambda x: x[0])
    return out


# ---------------------------------------------------------------------------
# Аргументация для existing ``run.run(args)``
# ---------------------------------------------------------------------------

def args_for(input_path: Path, output_final: Path, debug_path: Path, model: str,
             workers: int, retries: int, confidence_min: float,
             target_chunk_tokens: int, quiet: bool, no_pdf: bool) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        input=str(input_path),
        model=model,
        workers=workers,
        retries=retries,
        confidence_min=confidence_min,
        no_resplit=False,            # re-segmentation ENABLED (требование: не использовать --no-resplit)
        target_chunk_tokens=target_chunk_tokens,
        quiet=quiet,
        no_examples=False,
        no_pdf=no_pdf,
        output=str(output_final),
        debug=str(debug_path),
        debug_dir=None,
        first_segments=10,
    )


# ---------------------------------------------------------------------------
# Валидация final
# ---------------------------------------------------------------------------

REQUIRED_COLUMNS = [
    "store_id", "seller_id", "client_id", "recognition_text", "dialog_type",
    "is_sale", "loss_reason", "is_alarm_triggered", "dialog_start_at",
    "dialog_end_at", "dialog_duration_sec", "session_ids", "model_reasoning",
]


def validate_final_excel(final_path: Path) -> dict:
    """Возвращает dict с ``ok``, detail-строка и всеми требуемыми метриками."""
    res: dict = {"ok": True, "error": None, "customer_rows": 0,
                 "empty_dialog_type": 0, "empty_model_reasoning": 0,
                 "client_id_ok": True, "client_seq_ok": True,
                 "client_id_offline": 0}
    p = Path(final_path)
    if not p.exists():
        res["ok"] = False
        res["error"] = "final file missing"
        return res
    try:
        wb = load_workbook(filename=str(p), read_only=True, data_only=True)
        if "report" not in wb.sheetnames:
            res["ok"] = False
            res["error"] = "sheet 'report' not found"
            wb.close()
            return res
        df = pd.read_excel(p, sheet_name="report", engine="openpyxl")
        wb.close()
    except Exception as exc:  # noqa: BLE001
        res["ok"] = False
        res["error"] = f"cannot open final Excel: {exc}"
        return res

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        res["ok"] = False
        res["error"] = "missing columns: " + ",".join(missing)
        return res

    # customer rows — все строки в отчёте (по конвенции проекта: 1 строка = 1 client dialog)
    total_rows = len(df)
    res["total_rows"] = total_rows
    res["is_sale_count"] = int(df["is_sale"].fillna(False).astype(bool).sum()) if "is_sale" in df else 0
    res["dialog_type_dist"] = df["dialog_type"].fillna("").astype(str).value_counts().to_dict()

    if total_rows == 0:
        return res

    # customer rows (все строки отчёта) — нет «offline_...» в client_id
    cid = df["client_id"].astype(str)
    res["customer_rows"] = int((cid.str.strip().str.len().fillna(0) > 0).sum())
    res["client_id_offline"] = int(cid.str.startswith("offline_").sum())
    if res["client_id_offline"]:
        res["client_id_ok"] = False

    # пустые dialog_type у customer rows
    dt = df["dialog_type"].astype(str).str.strip()
    res["empty_dialog_type"] = int((dt == "").sum() + (dt == "nan").sum())

    # пустые model_reasoning
    mr = df["model_reasoning"].astype(str).str.strip()
    res["empty_model_reasoning"] = int((mr == "").sum() + (mr == "nan").sum())

    # последовательность client_id внутри магазина (client_1, client_2, ...)
    by_store: dict[str, list[int]] = {}
    for store, cid_cell in zip(df["store_id"].astype(str), df["client_id"].astype(str)):
        m = re.fullmatch(r"client_(\d+)", cid_cell or "")
        if not m:
            continue
        by_store.setdefault(store, []).append(int(m.group(1)))
    for nums in by_store.values():
        nums.sort()
        if nums != list(range(1, len(nums) + 1)):
            res["client_seq_ok"] = False
            break
    return res


def load_debug_reseg(debug_path: Path) -> dict:
    """Читает debug-JSON и извлекает re-segmentation метрики + coverage."""
    res = {"exists": False, "coverage_ok": None, "covered_rows": None,
           "total_input_rows": None, "long_segments": None,
           "split_events": None, "kept_single": None,
           "candidates": 0, "resegmented": 0, "llm_calls": 0}
    p = Path(debug_path)
    if not p.exists():
        return res
    res["exists"] = True
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return res
    c = d.get("coverage") or {}
    res["coverage_ok"] = c.get("ok")
    res["covered_rows"] = c.get("covered_rows")
    res["total_input_rows"] = d.get("total_input_rows")
    rs = d.get("re_long_split") or {}
    res["candidates"] = int(rs.get("candidates") or 0)
    res["resegmented"] = int(rs.get("resegmented") or 0)
    res["llm_calls"] = int(rs.get("llm_calls") or 0)
    split = rs.get("split_from") or []
    kept = rs.get("kept_single") or []
    res["split_events"] = len(split)
    res["kept_single"] = len(kept)
    # long segments = candidates (только те, что >= review-порога)
    res["long_segments"] = res["candidates"]
    return res


def coverage_lost_duplicates(debug: dict) -> tuple[int, int]:
    """Возвращает (lost_rows, duplicated_rows) на основе debug-coverage.

    ``write_debug`` уже проверяет ``set(covered) == set(range(total))`` и
    ``len(covered) == total_input_rows`` и пишет ``coverage.ok``. Поэтому:
      * lost   = исходных строк, которые не покрыты (total - covered, если covered<total);
      * dup    = дубликаты (covered > total).
    Если ``coverage.ok=True`` — оба значения 0.
    """
    if not debug or debug.get("coverage_ok") is None:
        return (0, 0)
    covered = debug.get("covered_rows") or 0
    total = debug.get("total_input_rows") or 0
    lost = max(0, total - covered)
    dup = max(0, covered - total)
    return (lost, dup)


# ---------------------------------------------------------------------------
# Обработка одного raw-файла
# ---------------------------------------------------------------------------

def process_one(
    date: str,
    raw_path: Path,
    reports_dir: Path,
    model: str,
    workers: int,
    retries: int,
    confidence_min: float,
    target_chunk_tokens: int,
    no_pdf: bool,
    quiet: bool,
) -> dict:
    final_target = reports_dir / f"final_transcript_report_{date}.xlsx"
    debug_target = reports_dir / f"final_transcript_report_{date}_debug.json"
    # ВАЖНО: staging в ТОМ ЖЕ каталоге (reports/), чтобы os.replace(a, b) был
    # атомарным (один и тот же FS). Имя temp оканчивается на ``.tmp.xlsx`` —
    # это входит в список допустимых расширений pandas ExcelWriter, и при том
    # явно НЕ является финальным названием.
    output_final = final_target.with_name(f"final_transcript_report_{date}.tmp.xlsx")
    debug_path = debug_target.with_name(f"final_transcript_report_{date}_tmp_debug.json")
    # чистим остатки предыдущих запусков: старые .tmp.xlsx и _tmp_debug.json
    for f in (reports_dir.glob(f"final_transcript_report_{date}.tmp.xlsx"),
              reports_dir.glob(f"final_transcript_report_{date}_tmp_debug.json")):
        for tmp in f:
            try:
                tmp.unlink()
            except OSError:
                pass

    print("=" * 60)
    print(f"Processing {date}")
    print("=" * 60)

    # RAW
    sheets = load_raw(str(raw_path))
    total_input_rows = sum(len(df) for _, df in sheets)
    stores = len(sheets)
    store_summaries = []
    for sid, df in sheets:
        store_summaries.append((sid, len(df)))
    print(f"RAW                : {raw_path}")
    print(f"INPUT ROWS         : {total_input_rows}  (stores={len(store_summaries)})")
    for sid, n in store_summaries:
        print(f"   - {sid}: {n} rows")

    args = args_for(
        raw_path, output_final, debug_path,
        model=model, workers=workers, retries=retries,
        confidence_min=confidence_min, target_chunk_tokens=target_chunk_tokens,
        quiet=quiet, no_pdf=no_pdf,
    )

    # Потоки — в лог
    t0 = time.time()
    buf = io.StringIO()
    rc = None
    try:
        with redirect_stdout(buf):
            rc = offline_run.run(args)
    except Exception as exc:  # noqa: BLE001
        print(f"  [ERR] run(): {exc!r}")
        print("=" * 60)
        return {"date": date, "status": "FAILED", "reason": f"run() raised {exc!r}",
                "output_final": str(final_target), "debug": str(debug_path),
                "stores": stores, "input_rows": total_input_rows, "rc": rc, "elapsed_sec": time.time() - t0}

    tail = buf.getvalue()
    print(tail)
    print(f"  pipeline rc      : {rc}")

    # --- VALIDATION: Excel ---
    v = validate_final_excel(output_final)
    print(f"  VALIDATION       : {'OK' if v['ok'] else 'FAIL (' + str(v['error']) + ')'}")
    print(f"    customer rows  : {v['customer_rows']}")
    if v.get("total_rows"):
        print(f"    total rows     : {v['total_rows']}")
        print(f"    is_sale count  : {v.get('is_sale_count')}")
        print(f"    dialog_type    : {v.get('dialog_type_dist')}")
        print(f"    empty dialog_type: {v['empty_dialog_type']}")
        print(f"    empty model_reasoning: {v['empty_model_reasoning']}")
        print(f"    client_id 'offline_' count: {v['client_id_offline']}")
        print(f"    client_id seq OK: {v['client_seq_ok']}")

    # --- DEBUG ---
    dbg = load_debug_reseg(debug_path)
    if dbg["exists"]:
        lost, dup = coverage_lost_duplicates(dbg)
        print(f"  DEBUG re-segmentation:")
        print(f"    input rows     : {dbg['total_input_rows']}")
        print(f"    coverage ok    : {dbg['coverage_ok']}  (covered={dbg['covered_rows']})")
        print(f"    long segs      : {dbg['long_segments']}")
        print(f"    candidates     : {dbg['candidates']}")
        print(f"    split events   : {dbg['split_events']} (kept single: {dbg['kept_single']})")
        print(f"    llm calls (re) : {dbg['llm_calls']}")
        print(f"    lost rows      : {lost}")
        print(f"    duplicated rows: {dup}")
    else:
        lost, dup = 0, 0
        print(f"  DEBUG            : MISSING at {debug_path}")

    # --- ATOMIC REPLACE ---
    # Критерий успеха: Excel валид + реального покрытия нет потерь/дублей.
    # ВАЖНО: ``write_debug`` пишет ``coverage.ok`` глобально по ВСЕМ магазинам дня,
    # а индексы строк в каждом магазине идут отдельно с 0. Для файлов с несколькими
    # магазинами эта глобальная проверка ИСТИННО ЛОЖНА, даже если реальное покрытие
    # в каждом магазине 100% (covered_rows == total_input_rows). Поэтому используем
    # ПРЯМУЮ проверку lost/dup (реальный инвариант), а не ``coverage.ok``.
    coverage_ok_effective = (not dbg["exists"]) or (lost == 0 and dup == 0)
    if v["ok"] and coverage_ok_effective:
        # успех: переносим temporary -> final на том же FS (atomic)
        reports_dir.mkdir(parents=True, exist_ok=True)
        os.replace(str(output_final), str(final_target))
        os.replace(str(debug_path), str(debug_target))
        print(f"  [OK] {date}")
        print("=" * 60)
        return {
            "date": date, "status": "OK", "output_final": str(final_target),
            "debug": str(debug_target),
            "stores": stores, "input_rows": total_input_rows, "elapsed_sec": time.time() - t0,
            "validation": v, "debug_metrics": dbg, "lost": lost, "duplicated": dup, "rc": rc,
        }

    # --- ПРОВАЛ: final НЕ трогаем; cleanup temp'ов ---
    if rc != 0:
        reason = f"pipeline rc={rc}"
    elif not v["ok"]:
        reason = v.get("error") or "final Excel invalid"
    elif dbg.get("exists") and (lost != 0 or dup != 0):
        reason = f"coverage FAILED (lost={lost}, dup={dup})"
    else:
        reason = "unknown"
    for tmp in (output_final, debug_path):
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    print(f"  [FAILED] {date} — {reason}")
    print("=" * 60)
    return {"date": date, "status": "FAILED", "reason": reason,
            "output_final": str(final_target), "debug": str(debug_target),
            "stores": stores, "input_rows": total_input_rows, "elapsed_sec": time.time() - t0,
            "validation": v, "debug_metrics": dbg, "rc": rc}


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------

def main() -> int:
    reports_dir = Path(os.getenv("REPORTS_DIR", str(_REPORTS)))
    model = os.getenv("OFFLINE_LLM_MODEL", "qwen3.8:27b")
    workers = int(os.getenv("OFFLINE_WORKERS", "1"))
    retries = int(os.getenv("OFFLINE_RETRIES", "2"))
    confidence_min = float(os.getenv("OFFLINE_CONFIDENCE_MIN", str(CONFIDENCE_MIN)))
    target_chunk_tokens = int(os.getenv("OFFLINE_TARGET_CHUNK_TOKENS", "36000"))
    no_pdf = os.getenv("BATCH_NO_PDF", "1") == "1"
    quiet = os.getenv("BATCH_QUIET", "1") == "1"

    print("OFFLINE BATCH RECALC")
    print(f"pipeline     : segmented + re-split (re-segmentation ENABLED)")
    print(f"model        : {model}")
    print(f"workers      : {workers}")
    print(f"retries      : {retries}")
    print(f"confidence_min: {confidence_min}")
    print(f"no_pdf       : {no_pdf}")
    rows_list = find_raw_files(reports_dir)
    only_dates = {x.strip() for x in os.getenv("BATCH_ONLY", "").split(",") if x.strip()}
    if only_dates:
        rows_list = [(d, p) for (d, p) in rows_list if d in only_dates]
        print(f"filter       : only {sorted(only_dates)}")
    print(f"raw files    : {len(rows_list)}")
    for d, p in rows_list:
        print(f"   {d}  {p.name}  {p.stat().st_size:,} bytes")
    print("=" * 60)
    print("BATCH BEGIN  (sequential, one raw file at a time)")
    print("=" * 60)

    results = []
    for date, raw_path in rows_list:
        r = process_one(
            date=date, raw_path=raw_path, reports_dir=reports_dir,
            model=model, workers=workers, retries=retries,
            confidence_min=confidence_min, target_chunk_tokens=target_chunk_tokens,
            no_pdf=no_pdf, quiet=quiet,
        )
        results.append(r)
    # --- SW summary ---
    print("=" * 60)
    print("BATCH COMPLETE")
    print("=" * 60)
    ok = [r for r in results if r["status"] == "OK"]
    failed = [r for r in results if r["status"] == "FAILED"]
    print(f"\nfiles total      : {len(results)}")
    print(f"files successful : {len(ok)}")
    print(f"files failed     : {len(failed)}")

    # Таблица
    print("\n--- Per-file summary ---")
    hdr = ("date", "stores", "input_rows", "customer_rows(final)", "long_segs",
           "reseg_split", "reseg_kept", "reseg_llm", "lost", "dup", "status")
    print(" | ".join(hdr))
    print("-" * 140)
    for r in results:
        v = r.get("validation") or {}
        d = r.get("debug_metrics") or {}
        print(" | ".join([
            r["date"],
            str(r.get("stores", "-")),
            str(r.get("input_rows", "-")),
            str(v.get("total_rows", "-")),
            str(d.get("long_segments", "-")),
            str(d.get("resegmented", "-")),
            str(d.get("kept_single", "-")),
            str(d.get("llm_calls", "-")),
            str(r.get("lost", "-")),
            str(r.get("duplicated", "-")),
            r["status"],
        ]))

    print("\n--- Failed files ---")
    for r in failed:
        print(f"  {r['date']}: {r.get('reason')}")

    # JSON-сводка (для машинной обработки) — не в reports/, чтобы не засорять отчёты
    summary_path = Path("/tmp/opencode/batch_summary.json")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsummary saved: {summary_path}")
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
