"""Offline-анализ дневного файла транскрипций.

Новый конвейер (без опоры на realtime ``client_id``):

    RAW EXCEL
        -> rows (0-based индексы, old client_id только для debug)
        -> SEGMENTATION (LLM, без client_id)
        -> CLASSIFICATION per dialog-segment (LLM)
        -> финальный Excel + debug JSON (old vs new)

Не затрагивает realtime-контур (ASR/VAD/Kafka/DB).

Запуск:
    python3 offline_analysis/run.py --input reports/transcript_report_YYYY-MM-DD.xlsx
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_STATS_DIR = Path(__file__).resolve().parents[1] / "stats_service"
if str(_STATS_DIR) not in sys.path:
    sys.path.insert(0, str(_STATS_DIR))

from client_numbering import assign_display_client_ids

from offline_analysis.llm import OllamaClient, OllamaError, extract_json
from offline_analysis.pdf_report import generate_pdf_report
from offline_analysis.prompt import (
    SYSTEM_PROMPT,
    build_classification_user_prompt,
    format_segment_with_times,
)
from offline_analysis.segmentation import (
    Segment,
    df_to_rows,
    duration_metrics,
    load_raw,
    log_size,
    measure_rows,
    segment_rows,
)
# EXPERIMENT: sales ground-truth (off by default; enabled via --sales-ground-truth)
from offline_analysis import sales_gt as _sales
from offline_analysis.taxonomy import (
    CONFIDENCE_MIN,
    LOSS_REASONS,
    MISSIONS,
    MISSION_TO_DIALOG_TYPE,
    ROLES,
    SEGMENT_TYPES,
)


# --- Бюджет LLM, согласованный с regression-прогоном ---------------------------
# Модель qwen3.8:27b — thinking-модель: чтобы она и рассуждала, и выдала JSON
# сегментации за один запрос, нужен большой num_predict и num_ctx. Это именно тот
# бюджет, которым был пересчитан исторический файл (regression_new): без него
# segmentation дегенерирует в построчный fallback (тысячи unknown-сегментов) и
# ночной отчёт теряет большинство диалогов. Переменные окружения позволяют
# переопределить бюджет (не меняя код).
PIPELINE_NUM_CTX = int(os.getenv("OFFLINE_NUM_CTX", "131072"))
PIPELINE_NUM_PREDICT = int(os.getenv("OFFLINE_NUM_PREDICT", "128000"))


class BigBudgetLLM:
    """Обёртка над OllamaClient: гарантирует минимальный бюджет контекста/выхода.

    Реализует тот же интерфейс (chat_json), что ожидает segmentation и
    classification. НЕ меняет prompt'ы, таксономию и логику segmentation — только
    поднимает num_ctx / num_predict до уровня, которым был сделан regression-прогон
    исторических файлов (реализация BigBudgetLLM там идентична по смыслу).
    """

    def __init__(self, model: str | None = None, min_ctx: int = PIPELINE_NUM_CTX,
                 min_predict: int = PIPELINE_NUM_PREDICT, min_timeout: int = 3600):
        self._c = OllamaClient(model=model, timeout=min_timeout)
        self.model = self._c.model
        self.host = self._c.host
        self._min_ctx = min_ctx
        self._min_predict = min_predict

    def chat_json(self, system: str, user: str, **kwargs) -> dict:
        kwargs["num_ctx"] = max(int(kwargs.get("num_ctx", 0)), self._min_ctx)
        kwargs["num_predict"] = max(int(kwargs.get("num_predict", 0)), self._min_predict)
        return self._c.chat_json(system, user, **kwargs)


REPORT_COLUMNS = [
    "store_id",
    "seller_id",
    "client_id",            # новый display client_id — назначается ПОСЛЕ segmentation
    "recognition_text",
    "dialog_type",
    "is_sale",
    "loss_reason",
    "is_alarm_triggered",
    "dialog_start_at",
    "dialog_end_at",
    "dialog_duration_sec",
    "session_ids",
    "model_reasoning",
]


# ---------------------------------------------------------------------------
# Нормализация результата LLM (классификации) — сохраняем существующую логику
# ---------------------------------------------------------------------------

def _clean_role(value) -> str:
    if not isinstance(value, str):
        return "unknown"
    value = value.strip().lower()
    return value if value in ROLES else "unknown"


def _clean_mission(value) -> str:
    if not isinstance(value, str):
        return "unknown"
    value = value.strip()
    for m in MISSIONS:
        if value.lower() == m.lower() or value.replace(" ", "") == m.replace(" ", ""):
            return m
    return "unknown"


def _clean_loss(value) -> str | None:
    if value is None or not isinstance(value, str):
        return None
    value = value.strip()
    for r in LOSS_REASONS:
        if value == r:
            return r
    norm = re.sub(r"\s+", " ", value.lower().replace("слэш", "/"))
    for r in LOSS_REASONS:
        if norm == re.sub(r"\s+", " ", r.lower()):
            return r
    return None


def normalize_llm_result(raw: dict) -> dict:
    """Нормализует ответ LLM-классификации (сохраняем существующую бизнес-логику)."""
    role = _clean_role(raw.get("role"))
    mission = _clean_mission(raw.get("mission"))
    is_sale = bool(raw.get("is_sale", False))
    loss = _clean_loss(raw.get("loss_reason"))
    try:
        confidence = float(raw.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = raw.get("reason")
    reason = reason if isinstance(reason, str) else None

    # бизнес-правила (требования 9-12 — оставляем как в старом коде)
    if role != "customer":
        is_sale = False
        loss = None
    if mission not in MISSIONS or mission not in MISSION_TO_DIALOG_TYPE:
        mission_norm = "unknown"
    else:
        mission_norm = mission

    if mission_norm == "Купить":
        if is_sale:
            loss = None
        # else — оставляем допустимую причину или None
    else:
        if mission_norm != "unknown":
            is_sale = False
        loss = None

    return {
        "role": role,
        "mission": mission_norm if mission_norm in MISSIONS else "unknown",
        "is_sale": bool(is_sale and mission_norm == "Купить"),
        "loss_reason": loss if (is_sale is False and mission_norm == "Купить" and role == "customer") else None,
        "confidence": confidence,
        "reason": reason,
        "dialog_type": MISSION_TO_DIALOG_TYPE.get(mission_norm),
        "raw": raw,
    }


# ---------------------------------------------------------------------------
# EXPERIMENT: sales ground-truth helpers (no-op if sales_gt is None)
# ---------------------------------------------------------------------------

def _sales_for_store(sales_gt, store_id: str | None) -> list:
    """Все real-sale записи для конкретного store_id (упорядоченные по времени)."""
    if not sales_gt:
        return []
    recs = [r for r in sales_gt if r.store_id == store_id and r.is_purchase]
    recs.sort(key=lambda r: r.sale_time)
    return recs


def _sales_snippet_for_store(sales_gt, store_id: str | None) -> str | None:
    if not sales_gt:
        return None
    recs = _sales_for_store(sales_gt, store_id)
    if not recs:
        return None
    return _sales.make_sales_snippet(recs, store_id or "")


# ---------------------------------------------------------------------------
# Классификация одного dialog-сегмента (ПОСЛЕ segmentation)
# ---------------------------------------------------------------------------

def _attempt_one_classification(
    client, segment: Segment, block_text: str | None,
    sales_snippet: str | None = None,
) -> dict:
    prompt = build_classification_user_prompt(
        store_id=segment.store_id,
        seller_id=segment.seller_id,
        segment_text_with_times=block_text if block_text is not None else format_segment_with_times(segment),
        sales_snippet=sales_snippet,
    )
    out = client.chat_json(SYSTEM_PROMPT, prompt)
    parsed = extract_json(out["content"])
    if not parsed:
        raise ValueError(f"no JSON in LLM response: {out['content'][:160]!r}")
    return normalize_llm_result(parsed)


def classify_segment(
    client: OllamaClient,
    segment: Segment,
    index: int,
    retries: int = 2,
    verbose: bool = True,
    sales_snippet: str | None = None,
) -> dict:
    """Классифицирует ОДИН выделенный dialog-сегмент."""
    last_error = None
    for attempt in range(retries + 1):
        try:
            text = format_segment_with_times(segment)
            parsed = _attempt_one_classification(client, segment, text, sales_snippet)
            parsed["segment_index"] = index
            if verbose:
                print(
                    f"[CLS] seg#{index:02d} {segment.store_id[:24]} "
                    f"rows={segment.row_count} span={segment.duration_sec:5.0f}s "
                    f"role={parsed['role']} mission={parsed['mission']} "
                    f"sale={parsed['is_sale']} loss={parsed['loss_reason']} conf={parsed['confidence']:.2f}"
                )
            return parsed
        except (OllamaError, ValueError) as exc:
            last_error = exc
            if verbose:
                print(f"[ERR] seg#{index:02d} attempt {attempt + 1}: {exc}")
            time.sleep(5)

    if verbose:
        print(f"[SKIP-MODEL] seg#{index:02d} — не удалось получить результат: {last_error}")
    return {
        "segment_index": index,
        "role": "unknown",
        "mission": "unknown",
        "is_sale": False,
        "loss_reason": None,
        "confidence": 0.0,
        "reason": f"LLM error: {last_error}",
        "dialog_type": None,
        "raw": None,
        "llm_error": str(last_error),
    }


# ---------------------------------------------------------------------------
# Финальный отчёт
# ---------------------------------------------------------------------------

def build_final_rows(
    segments: list[Segment],
    all_results: dict[int, dict],
    confidence_min: float,
) -> tuple[list[dict], list[dict]]:
    """Возвращает (rows_для_отчёта, отклонённые сегменты).

    1 строка отчёта = 1 клиентский диалог-сегмент.
    display client_id (client_001, client_002, ...) назначается ПОСЛЕ
    segmentation (через assign_display_client_ids), а НЕ из старого realtime ID.
    """
    rows = []
    rejected = []

    for idx, segment in enumerate(segments):
        r = all_results.get(idx)
        if r is None or r.get("llm_error") or r["role"] != "customer":
            if r is not None:
                rejected.append({
                    "segment_index": idx,
                    "segment_id": segment.segment_id,
                    "segment_type": segment.segment_type,
                    "role": r.get("role"),
                    "mission": r.get("mission"),
                    "confidence": r.get("confidence"),
                    "reason": r.get("reason"),
                    "llm_error": r.get("llm_error"),
                })
            continue

        text = segment.text
        session_ids = ", ".join(segment.session_ids)
        start = segment.start_at
        end = segment.end_at
        duration = segment.duration_sec
        dt = r.get("dialog_type")
        if not dt or not str(dt).strip():
            dt = MISSION_TO_DIALOG_TYPE["Прочее"]

        rows.append({
            "store_id": segment.store_id,
            "seller_id": segment.seller_id,
            "client_id": None,
            "identity": segment.segment_id,  # уникальный ключ сегмента (уникален в пределах магазина)
            "recognition_text": text,
            "dialog_type": dt,
            "is_sale": bool(r["is_sale"]),
            "loss_reason": r["loss_reason"],
            "is_alarm_triggered": bool(segment.alarm_any),
            "dialog_start_at": start,
            "dialog_end_at": end,
            "dialog_duration_sec": round(duration, 3),
            "session_ids": session_ids,
            "model_reasoning": r.get("reason") if isinstance(r.get("reason"), str) and r.get("reason", "").strip() else None,
        })

    # display client_id назначается ПОСЛЕ segmentation: последовательность
    # внутри магазина по уникальному ключу сегмента (segment_XXX).
    rows = assign_display_client_ids(rows, identity=lambda row: row.get("identity") or "")
    for row in rows:
        row.pop("identity", None)

    rows.sort(key=lambda x: (x["store_id"], x.get("dialog_start_at") or datetime.min))
    return rows, rejected


# ---------------------------------------------------------------------------
# Запись Excel и debug
# ---------------------------------------------------------------------------

def write_excel(rows: list[dict], output_path: str) -> None:
    if not rows:
        # создаём пустой файл со схемой, чтобы downstream не падал
        out = pd.DataFrame(columns=REPORT_COLUMNS)
    else:
        out = pd.DataFrame(rows)
        for c in REPORT_COLUMNS:
            if c not in out.columns:
                out[c] = None
        out = out[REPORT_COLUMNS]
        for col in ("dialog_start_at", "dialog_end_at"):
            out[col] = pd.to_datetime(out[col], errors="coerce").dt.tz_localize(None)
        out["is_sale"] = out["is_sale"].fillna(False).astype(bool)
        out["is_alarm_triggered"] = out["is_alarm_triggered"].fillna(False).astype(bool)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        out.to_excel(writer, index=False, sheet_name="report")
        ws = writer.sheets["report"]
        for row in ws.iter_rows():
            for cell in row:
                cell.alignment = cell.alignment.copy(wrap_text=True)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def write_debug(
    segments: list[Segment],
    all_results: dict[int, dict],
    rejected: list[dict],
    total_input_rows: int,
    day_size: dict,
    debug_path: str,
    extra: dict | None = None,
) -> None:
    """Расширенный debug: покрывает ВСЕ сегменты дня (dialog/employee/background/unknown)."""
    items = []
    for idx, segment in enumerate(segments):
        r = all_results.get(idx)
        items.append({
            "segment_id": segment.segment_id,
            "store_id": segment.store_id,
            "start_at": _iso(segment.start_at),
            "end_at": _iso(segment.end_at),
            "start_row": segment.start_index,
            "end_row": segment.end_index,
            "row_count": segment.row_count,
            "old_client_ids": segment.old_client_ids,
            "segment_type": segment.segment_type,
            "segmentation_confidence": segment.confidence,
            "segmentation_reason": segment.reason,
            "classification_role": r["role"] if r else None,
            "classification_mission": r["mission"] if r else None,
            "classification_confidence": r["confidence"] if r else None,
            "is_sale": r["is_sale"] if r else None,
            "loss_reason": r["loss_reason"] if r else None,
            "recognition_text": segment.text,
            "llm_error": r.get("llm_error") if r else None,
        })

    # покрытие: сумма row_count по сегментам == все строки дня (без потерь и дублей).
    # ВАЖНО: индексы start_index/end_index сегментов — PER-STORE (см. segmentation.py),
    # union по ним в пересчёте покрытия НЕКОНКУРЕНТЕН для нескольких магазинов;
    # корректная метрика «нет lost/dup» — сумма row_count.
    covered_rows_sum = sum(
        s.row_count if getattr(s, "row_count", None) is not None
        else (s.end_index - s.start_index + 1)
        for s in segments
    )
    coverage_ok = (covered_rows_sum == total_input_rows)

    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_input_rows": total_input_rows,
        "day_size": day_size,
        "total_segments": len(segments),
        "segment_type_counts": _count_types(segments),
        "accepted_customers": sum(1 for it in items if it["classification_role"] == "customer"),
        "rejected_segments": len(rejected),
        "coverage": {
            "ok": coverage_ok,
            "covered_rows": covered_rows_sum,
            "total_rows": total_input_rows,
        },
        "dialog_duration_metrics": duration_metrics(segments),
        "re_long_split": (extra or {}).get("re_long_split"),
        "segments": items,
        "rejected_details": rejected,
    }
    Path(debug_path).parent.mkdir(parents=True, exist_ok=True)
    with open(debug_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _count_types(segments: list[Segment]) -> dict:
    out: dict[str, int] = {}
    for s in segments:
        key = s.segment_type or "unknown"
        out[key] = out.get(key, 0) + 1
    return out


# ---------------------------------------------------------------------------
# Stats / примеры
# ---------------------------------------------------------------------------

def print_stats(
    total_rows: int,
    day_size: dict,
    segments: list[Segment],
    all_results: dict[int, dict],
    final_rows: list[dict],
    input_file: str,
    output_file: str,
) -> None:
    role_counts: dict[str, int] = {}
    for r in all_results.values():
        role_counts[r["role"]] = role_counts.get(r["role"], 0) + 1

    customers = [r for r in all_results.values() if r["role"] == "customer"]
    buy = [r for r in customers if r["mission"] == "Купить"]
    loss_counts: dict[str | None, int] = {}
    for r in buy:
        if not r["is_sale"]:
            key = r["loss_reason"] or "(null)"
            loss_counts[key] = loss_counts.get(key, 0) + 1

    dt_counts: dict[str, int] = {}
    for r in final_rows:
        dt_counts[r["dialog_type"]] = dt_counts.get(r["dialog_type"], 0) + 1

    print("\n" + "=" * 70)
    print("СТАТИСТИКА OFFLINE-АНАЛИЗА")
    print("=" * 70)
    print(f"Исходный файл            : {input_file}")
    print(f"Финальный отчёт          : {output_file}")
    print(f"ASR-записей (строк)      : {total_rows}")
    print(f"  characters             : {day_size.get('characters')}")
    print(f"  estimated_tokens       : {day_size.get('estimated_tokens')}")
    print(f"Сегментов дня            : {len(segments)}")
    print("  по типам:")
    for key, val in sorted(_count_types(segments).items()):
        print(f"    - {key:<10} : {val}")
    print("Классификация (только dialog-сегменты):")
    print(f"  role=customer : {role_counts.get('customer', 0)}")
    print(f"  role=employee : {role_counts.get('employee', 0)}")
    print(f"  role=background: {role_counts.get('background', 0)}")
    print(f"  role=unknown  : {role_counts.get('unknown', 0)}")
    print(f"customer с миссией      : {sum(1 for r in customers if r['mission'] != 'unknown')}")
    print(f"customer, mission=Купить: {len(buy)}")
    print(f"  buy, is_sale=true    : {sum(1 for r in buy if r['is_sale'])}")
    print(f"  buy, is_sale=false   : {sum(1 for r in buy if not r['is_sale'])}")
    print("  loss_reason по buy-без-покупки:")
    if loss_counts:
        for k, v in sorted(loss_counts.items(), key=lambda x: -x[1]):
            print(f"    - {k}: {v}")
    else:
        print("    (нет)")
    print("dialog_type в финальном отчёте:")
    for k, v in sorted(dt_counts.items(), key=lambda x: str(x[0])):
        print(f"    - {k}: {v}")
    print(f"Строк в финальном отчёте : {len(final_rows)}")
    print("=" * 70)


def show_first_segments(segments: list[Segment], all_results: dict[int, dict], n: int = 10) -> None:
    print("\n" + "=" * 70)
    print(f"ПЕРВЫЕ {min(n, len(segments))} СЕГМЕНТОВ ДНЯ (new offline segmentation)")
    print("=" * 70)
    for i, seg in enumerate(segments[:n]):
        r = all_results.get(i)
        role = r.get("role") if r else "-"
        short_reason = (seg.reason or "").replace("\n", " ")
        if len(short_reason) > 90:
            short_reason = short_reason[:87] + "..."
        print(
            f"  {seg.segment_id}  "
            f"{seg.start_at.strftime('%H:%M:%S') if seg.start_at else '-'} -> "
            f"{seg.end_at.strftime('%H:%M:%S') if seg.end_at else '-'}  "
            f"rows={seg.row_count:<4} "
            f"type={seg.segment_type:<10} "
            f"seg_conf={seg.confidence:.2f}  "
            f"role={role:<10}  "
            f"| {short_reason}"
        )


# ---------------------------------------------------------------------------
# EXPERIMENT: sales ground-truth — сопоставление и post-LLM enforcement
# (включается ТОЛЬКО при args.sales_ground_truth; без флага ничего не меняется)
# ---------------------------------------------------------------------------

def _customer_index_map(segments: list[Segment], all_results: dict) -> dict:
    """Воспроизводит нумерацию display client_id из build_final_rows / assign_display_client_ids:

    возвращает ``{(store_id, client_id): segment_index}`` для customer-сегментов,
    в порядке перечисления сегментов (то, что попадает в финальный отчёт).
    """
    per_store: dict[str, int] = {}
    out: dict[tuple[str, str], int] = {}
    for idx, seg in enumerate(segments):
        r = all_results.get(idx)
        if r is None or r.get("llm_error") or r.get("role") != "customer":
            continue
        store = seg.store_id
        n = per_store.get(store, 0) + 1
        per_store[store] = n
        out[(store, f"client_{n}")] = idx
    return out


def enforce_ground_truth(
    segments: list[Segment],
    all_results: dict[int, dict],
    final_rows: list[dict],
    sales_gt: list,
    candidate_window_sec: int,
    verbose: bool = True,
) -> list[dict]:
    """Post-LLM контроль (EXPERIMENT): привязывает реальные продажи к dialog.

    * Продажа, попавшая в окно диалога (buy/is_sale) и не покрытая другой продажей
      -> этот диалог принудительно mission=«Купить», is_sale=true, dialog_type=buy.
    * Продажа, у которой есть candidate-диалог, но он НЕ buy, и это ближайший
      sales-кандидат -> принудительно buy (исправление WRONG_TYPE).

    Не меняет диалоги, которым уже соответствует другая (ближе) продажа.
    Возвращает список log-записей применённых коррекций.
    """
    if not sales_gt:
        return []
    results = _sales.match_sales_to_dialogs(
        sales_gt, final_rows,
        candidate_window_sec=candidate_window_sec,
    )
    cust_idx = _customer_index_map(segments, all_results)

    # Какие продажи уже «закрывают» каждый диалог (по MATCHED/AMBIGUOUS)
    covered_by: dict[tuple[str, str], list[int]] = {}
    for res in results:
        if res.matched_dialog is None:
            continue
        if res.status in ("MATCHED", "AMBIGUOUS"):
            covered_by.setdefault((res.store_id, res.matched_dialog), []).append(res.sale.id)

    corrections: list[dict] = []
    # 1) Принудительно buy для диалогов, куда привязана продажа (MATCHED/AMBIGUOUS)
    for res in results:
        if res.matched_dialog is None:
            continue
        if res.status not in ("MATCHED", "AMBIGUOUS"):
            continue
        key = (res.store_id, res.matched_dialog)
        seg_idx = cust_idx.get(key)
        if seg_idx is None:
            continue
        row = [r for r in final_rows
               if r.get("store_id") == res.store_id and r.get("client_id") == res.matched_dialog]
        if not row:
            continue
        already = row[0].get("dialog_type") == "buy" and bool(row[0].get("is_sale"))
        if not already:
            all_results[seg_idx]["mission"] = "Купить"
            all_results[seg_idx]["is_sale"] = True
            all_results[seg_idx]["dialog_type"] = "buy"
            all_results[seg_idx]["loss_reason"] = None
            if all_results[seg_idx].get("confidence", 0.0) < 0.8:
                all_results[seg_idx]["confidence"] = 0.8
            row[0]["dialog_type"] = "buy"
            row[0]["is_sale"] = True
            row[0]["loss_reason"] = None
            all_results[seg_idx]["enforced_by_ground_truth"] = [res.sale.id]
            corrections.append({
                "action": "ENFORCE_BUY",
                "store_id": res.store_id,
                "client_id": res.matched_dialog,
                "segment_index": seg_idx,
                "sale_ids": covered_by.get(key, [res.sale.id]),
            })
            if verbose:
                print(
                    f"[SALES-GT] ENFORCE_BUY {res.store_id}/{res.matched_dialog} "
                    f"sale={res.sale.sale_time:%H:%M:%S} ({res.sale.id}) "
                    f"(was {row[0].get('dialog_type')}/{row[0].get('is_sale')})"
                )
    # 2) WRONG_TYPE: кандидат есть, но он не buy — если диалог NEAR и sales-кандидат,
    #    и не покрыт другой продажей -> принудительно buy.
    for res in results:
        if res.status != "WRONG_TYPE":
            continue
        if res.matched_dialog is None:
            continue
        key = (res.store_id, res.matched_dialog)
        # не трогаем диалог, который уже закрывает другую (ближе) продажу
        if key in covered_by:
            continue
        seg_idx = cust_idx.get(key)
        if seg_idx is None:
            continue
        row = [r for r in final_rows
               if r.get("store_id") == res.store_id and r.get("client_id") == res.matched_dialog]
        if not row:
            continue
        all_results[seg_idx]["mission"] = "Купить"
        all_results[seg_idx]["is_sale"] = True
        all_results[seg_idx]["dialog_type"] = "buy"
        all_results[seg_idx]["loss_reason"] = None
        if all_results[seg_idx].get("confidence", 0.0) < 0.8:
            all_results[seg_idx]["confidence"] = 0.8
        row[0]["dialog_type"] = "buy"
        row[0]["is_sale"] = True
        row[0]["loss_reason"] = None
        all_results[seg_idx]["enforced_by_ground_truth"] = [res.sale.id]
        corrections.append({
            "action": "ENFORCE_BUY_WRONGTYPE",
            "store_id": res.store_id,
            "client_id": res.matched_dialog,
            "segment_index": seg_idx,
            "sale_ids": [res.sale.id],
            "sale_time": res.sale.sale_time.isoformat(),
            "dialog_type_before": row[0].get("dialog_type"),
        })
        if verbose:
            print(
                f"[SALES-GT] ENFORCE_BUY_WRONGTYPE {res.store_id}/{res.matched_dialog} "
                f"sale={res.sale.sale_time:%H:%M:%S} (was {row[0].get('dialog_type')})"
            )
    return corrections


def run(args) -> int:
    """Выполняет offline-конвейер и создаёт отчёты (Excel + PDF).

    Возвращает код завершения: 0 — успех, 1 — ошибка при создании PDF
    (Excel при этом СОХРАНЯЕТСЯ и не удаляется; проблема явно выводится).
    Логика сегментации/классификации и бизнес-правила не затрагиваются.
    """
    input_file = args.input
    output_file = args.output or input_file_path_to_final(input_file)
    output_file = str(Path(output_file).expanduser())
    # PDF-отчёт: тот же путь, расширение .pdf (рядом с Excel)
    output_pdf = str(Path(output_file).with_suffix(".pdf"))
    # Дата отчёта из имени файла (YYYY-MM-DD); fallback — текущая (как в input_file_path_to_final)
    output_date = extract_report_date(output_file)
    debug_path = args.debug or str(Path(output_file).with_name(Path(output_file).stem + "_debug.json"))

    # 1) SEGMENTATION (без client_id)
    client = BigBudgetLLM(model=args.model)

    # --- EXPERIMENT: sales ground-truth (только если задан --sales-ground-truth) ---
    sales_gt: list = []
    sales_meta: dict | None = None
    if getattr(args, "sales_ground_truth", None):
        report_date = extract_report_date(output_file or args.input)
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", report_date)
        gt_day = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else datetime.now()
        sales_gt, sales_meta = _sales.load_sales_ground_truth(args.sales_ground_truth, day=gt_day)
        print("SALES-GROUND-TRUTH (EXPERIMENT MODE)")
        print(f"  file           : {args.sales_ground_truth}")
        print(f"  records        : {sales_meta['n_records']} "
              f"(покупок={sales_meta['n_purchase']}, возвратов={sales_meta['n_return']})")
        print(f"  stores in GT   : {sales_meta['stores_sorted']}")
        print(f"  candidate_win  : +/-{args.sales_candidate_window}s")
        print(f"  core_window    : +/-{args.sales_window}s")

    # --- Явный баннер пайплайна: чтобы нельзя было спутать новый и старый offline ---
    # (требование: в nightly-логе должно быть видно, какой именно pipeline запущен).
    print("OFFLINE ANALYSIS")
    print(f"pipeline     : segmented (LLM segmentation -> LLM classification)")
    print(f"model        : {client.model}")
    print(f"host         : {client.host}")
    print(f"num_ctx      : {PIPELINE_NUM_CTX}")
    print(f"num_predict  : {PIPELINE_NUM_PREDICT}")
    print(f"workers      : {args.workers}")
    print(f"retries      : {args.retries}")
    print(f"confidence_min: {args.confidence_min}")
    print(f"resplit_long : {'on' if args.resplit else 'off (default; holistic prompt handles it)'}")
    print(f"input        : {input_file}")
    print(f"output       : {output_file}")
    print(f"output_pdf   : {output_pdf}")

    sheets = load_raw(input_file)
    total_rows = sum(len(df) for _, df in sheets)
    store_rows = []
    for store_id, df in sheets:
        store_rows.append((store_id, df_to_rows(df)))
    print(f"ASR-записей: {total_rows} (магазинов: {len(store_rows)})")

    all_segments: list[Segment] = []
    all_results: dict[int, dict] = {}
    day_size = {"rows": 0, "characters": 0, "estimated_tokens": 0}
    resplit_sink: dict = {}
    for store_id, rows in store_rows:
        size = measure_rows(rows)
        log_size(f"STORE {store_id}", size)
        day_size["rows"] += size.rows
        day_size["characters"] += size.characters
        day_size["estimated_tokens"] += size.estimated_tokens
        _store_snippet = _sales_snippet_for_store(sales_gt, store_id) if sales_gt else None
        segments = segment_rows(
            rows,
            llm=client,
            store_id=store_id,
            target_chunk_tokens=args.target_chunk_tokens,
            verbose=not args.quiet,
            retries=args.retries,
            resplit_long=args.resplit,
            metrics_sink=resplit_sink,
            sales_snippet=_store_snippet,
        )
        all_segments.extend(segments)
    print(f"Сегментация: {len(all_segments)} сегментов дня")

    # Контроль качества: сколько подозрительно длинных dialog-сегментов проверено
    # и сколько из них разбито на несколько клиентов (новый механизм re-split).
    if resplit_sink.get("candidates"):
        print(
            f"Re-split длинных диалогов: {resplit_sink.get('candidates')} проверено, "
            f"{resplit_sink.get('resegmented')} разбито "
            f"(LLM-вызовов: {resplit_sink.get('llm_calls')})"
        )
    dur_metrics = duration_metrics(all_segments)
    if dur_metrics.get("count"):
        print(
            f"Длительности dialog: max={dur_metrics['max_sec'] / 60:.0f}м "
            f"p95={dur_metrics['p95_sec'] / 60:.0f}м "
            f">30м={dur_metrics['gt_30min']} >60м={dur_metrics['gt_60min']} "
            f">90м={dur_metrics['gt_90min']} >120м={dur_metrics['gt_120min']}"
        )

    # 2) CLASSIFICATION ТОЛЬКО dialog-сегментов (после segmentation)
    targets = [
        (idx, seg) for idx, seg in enumerate(all_segments) if seg.segment_type == "dialog"
    ]
    if len(targets) > 1 and args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {
                pool.submit(
                    classify_segment, client, seg, idx, args.retries, not args.quiet,
                    _sales_snippet_for_store(sales_gt, seg.store_id) if sales_gt else None,
                ): idx
                for idx, seg in targets
            }
            for fut in as_completed(futs):
                idx = futs[fut]
                all_results[idx] = fut.result()
    else:
        for idx, seg in targets:
            all_results[idx] = classify_segment(
                client, seg, idx, args.retries, not args.quiet,
                _sales_snippet_for_store(sales_gt, seg.store_id) if sales_gt else None,
            )

    # 3) финальный отчёт (display client_id назначается ПОСЛЕ segmentation)
    final_rows, rejected = build_final_rows(all_segments, all_results, args.confidence_min)

    # --- EXPERIMENT: sales ground-truth postprocessing ---
    # Сопоставляем sales-записи с final-диалогами. Применяем принудительные
    # поправки (ENFORCE_BUY / ENFORCE_BUY_WRONGTYPE) и пишем sales_matching_*.xlsx.
    # Без --sales-ground-truth ничего не происходит.
    sales_results = None
    sales_metrics = None
    sales_corrections: list[dict] = []
    if sales_gt:
        # 1-й pass: "до применения принудительных поправок" — фиксируем status,
        # чтобы в отчёте видеть, какие диалоги были «не-buy» до коррекции.
        sales_results = _sales.match_sales_to_dialogs(
            sales_gt, final_rows,
            core_window_sec=args.sales_window,
            candidate_window_sec=args.sales_candidate_window,
        )
        # применяем принудительные поправки (mutates all_results + final_rows)
        sales_corrections = enforce_ground_truth(
            all_segments, all_results, final_rows, sales_gt,
            candidate_window_sec=args.sales_candidate_window,
            verbose=not args.quiet,
        )
        # 2-й pass: после принудительных поправок — пересчитываем статус.
        sales_results = _sales.match_sales_to_dialogs(
            sales_gt, final_rows,
            core_window_sec=args.sales_window,
            candidate_window_sec=args.sales_candidate_window,
        )
        sales_metrics = _sales.sales_metrics(sales_results, final_rows)
        print("\nSALES-GROUND-TRUTH — METRICS")
        for k, v in sales_metrics.items():
            print(f"  {k:22}: {v}")
        # пишем sales_matching_<date>.xlsx
        report_date = extract_report_date(output_file)
        matching_path = str(Path(output_file).parent / "sales_matching_" + report_date + ".xlsx")
        _sales.write_matching_xlsx(
            sales_results, final_rows, matching_path,
            meta=sales_meta, metrics=sales_metrics,
        )
        print(f"SALES-MATCHING-REPORT: {matching_path}")

    write_excel(final_rows, output_file)
    write_debug(
        all_segments, all_results, rejected, total_rows, day_size, debug_path,
        extra={"re_long_split": resplit_sink or None},
    )
    if args.debug_dir:
        Path(args.debug_dir).mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy(debug_path, str(Path(args.debug_dir) / Path(debug_path).name))

    # 3.5) PDF-отчёт (те же final_rows, без повторного LLM-запуска).
    # ВАЖНО: Excel — критический артефакт и уже создан выше. Если PDF не удалось
    # создать — НЕ удаляем Excel, явно выводим ошибку и отражаем проблему в exit-code,
    # но НЕ маскируем исключение. Статистика/примеры печатаются ВСЕГДА.
    pdf_ok = True
    if not getattr(args, "no_pdf", False):
        try:
            pdf_path = generate_pdf_report(final_rows, output_pdf, report_date=output_date)
            print(f"PDF-отчёт создан: {pdf_path}")
        except Exception as exc:  # noqa: BLE001 — не проглатываем, явно сообщаем
            pdf_ok = False
            print(
                f"[ERR] PDF-отчёт не создан: {exc!r}",
                file=sys.stderr,
            )
            print(
                f"    Excel сохранён: {output_file}",
                file=sys.stderr,
            )

    # 4) статистика + первичные сегменты
    print_stats(total_rows, day_size, all_segments, all_results, final_rows, input_file, output_file)
    if not args.no_examples:
        show_first_segments(all_segments, all_results, n=args.first_segments)

    if pdf_ok:
        print("\nDONE")
    else:
        print("\nDONE (PDF generation failed — see [ERR] above)", file=sys.stderr)
        return 1
    return 0


def input_file_path_to_final(input_file: str) -> str:
    p = Path(input_file)
    m = re.search(r"(\d{4}-\d{2}-\d{2})", p.stem)
    date_part = m.group(1) if m else time.strftime("%Y-%m-%d")
    return str(p.parent / f"final_transcript_report_{date_part}.xlsx")


def extract_report_date(output_file: str) -> str:
    """Извлекает дату (YYYY-MM-DD) из имени файла отчёта.

    Используют её и для PDF-заголовка. Если в имени нет даты — текущая дата,
    что согласуется с поведением ``input_file_path_to_final``.
    """
    m = re.search(r"(\d{4}-\d{2}-\d{2})", Path(output_file).stem)
    return m.group(1) if m else time.strftime("%Y-%m-%d")


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline-анализ дневных транскрипций + финальный Excel-отчёт.")
    parser.add_argument("--input", default=None, help="сырой дневной Excel с транскрипциями")
    parser.add_argument("--output", default=None, help="путь к финальному Excel")
    parser.add_argument("--model", default="qwen3.8:27b", help="модель Ollama")
    # ТЕХНИЧЕСКИЕ параметры чанкинга (НЕ жёсткие правила разделения диалогов):
    parser.add_argument(
        "--target-chunk-tokens",
        type=int,
        default=40000,
        help="максимальный объём chunks в токенах (по умолчанию 40k — один вызов на магазин "
             "до ~30k строк, два на ~60k, три на ~90k; выше — LLM не выдаёт корректный JSON "
             "на CPU-инференсе, ниже — больше стыковых эффектов)",
    )
    # legacy-параметры, УДАЛЕНЫ как жёсткие правила (требование №5).
    parser.add_argument(
        "--gap-sec", type=float, default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--max-sec", type=float, default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--confidence-min", type=float, default=CONFIDENCE_MIN, help="минимальная уверенность LLM")
    parser.add_argument("--workers", type=int, default=1, help="параллельные запросы к LLM")
    parser.add_argument("--retries", type=int, default=2, help="повторы запроса к LLM")
    parser.add_argument("--debug", default=None, help="путь к debug JSON")
    parser.add_argument("--debug-dir", default=None, help="доп. каталог для debug-копий")
    parser.add_argument("--first-segments", type=int, default=10, help="сколько первых сегментов показать")
    parser.add_argument("--quiet", action="store_true", help="не печатать построчно")
    parser.add_argument("--no-examples", action="store_true", help="не показывать сегменты")
    parser.add_argument(
        "--no-pdf", action="store_true",
        help="пропустить генерацию PDF-отчёта (Excel создаётся всегда)",
    )
    parser.add_argument(
        "--resplit", action="store_true",
        help="включить ДОПОЛНИТЕЛЬНЫЙ LLM-проход re-split длинных dialog-сегментов "
             "(по умолчанию ВЫКЛЮЧЁН: основной holistic-промт сам решает границы)",
    )
    # --- EXPERIMENT: sales ground-truth (off by default; без флага pipeline работает как раньше) ---
    parser.add_argument(
        "--sales-ground-truth", default=None,
        help="EXPERIMENT: путь к Excel 'Документы продаж' (ground truth). "
             "Без этого флага поведение pipeline не меняется.",
    )
    parser.add_argument(
        "--sales-window", type=int, default=300,
        help="EXPERIMENT: core-окно (сек) вокруг времени продажи для candidate-диалога (по умолчанию 300)",
    )
    parser.add_argument(
        "--sales-candidate-window", type=int, default=600,
        help="EXPERIMENT: candidate-окно (сек); диалог попадает в candidate если sale внутри "
             "[start-окно, end+окно] (по умолчанию 600)",
    )
    args = parser.parse_args()

    if not args.input:
        default = Path(__file__).resolve().parents[1] / "reports"
        if (default / "transcript_report_latest.xlsx").exists():
            args.input = str(default / "transcript_report_latest.xlsx")
        else:
            files = sorted(default.glob("transcript_report_*.xlsx")) if default.exists() else []
            files = [f for f in files if not f.name.startswith("final_")]
            if not files:
                parser.error("--input не задан и не найден автоматический файл")
            args.input = str(files[-1])

    return run(args)


if __name__ == "__main__":
    sys.exit(main())
