"""Offline-анализ дневного файла транскрипций.

Цепочка:
    сырой Excel (по 1 строке ASR-сессию)
        -> segmentation (client_id + паузы)
        -> Qwen3.8 (Ollama) классифицирует каждый блок:
             role / mission / is_sale / loss_reason / confidence / reason
        -> финальный Excel final_transcript_report_YYYY-MM-DD.xlsx (лишь customer-блоки)

Не затрагивает realtime-контур (ASR/VAD/Kafka/DB) — только чтение сырого файла и запись Excel.

Запуск:
    python3 offline_analysis/run.py --input reports/transcript_report_2026-08-25.xlsx
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_STATS_DIR = Path(__file__).resolve().parents[1] / "stats_service"
if str(_STATS_DIR) not in sys.path:
    sys.path.insert(0, str(_STATS_DIR))

from client_numbering import assign_display_client_ids

from offline_analysis.llm import OllamaClient, OllamaError, extract_json
from offline_analysis.prompt import SYSTEM_PROMPT, build_user_prompt, format_block_with_times
from offline_analysis.segmentation import Block, build_blocks, df_to_rows, load_raw
from offline_analysis.taxonomy import (
    CONFIDENCE_MIN,
    LOSS_REASONS,
    MISSIONS,
    MISSION_TO_DIALOG_TYPE,
    ROLES,
)


REPORT_COLUMNS = [
    "store_id",
    "seller_id",
    "client_id",
    # "internal_client_id" is intentionally EXCLUDED from the user-facing report.
    # It remains an internal/debug field kept on the row dicts (see build_final_rows /
    # write_debug) and used by stats_service/client_numbering, but must NOT be exported
    # to the final Excel (agreed 13-column schema).
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
# Нормализация результата LLM
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
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    for r in LOSS_REASONS:
        if value == r:
            return r
    # допускаем мелкие расхождения по пробелам/слэшам
    norm = re.sub(r"\s+", " ", value.lower().replace("слэш", "/"))
    for r in LOSS_REASONS:
        if norm == re.sub(r"\s+", " ", r.lower()):
            return r
    return None


def normalize_llm_result(raw: dict, block: Block) -> dict:
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

    # бизнес-правила (требования 9-12)
    if role != "customer":
        is_sale = False
        loss = None
        if mission != "unknown":
            mission = mission  # храним для отладки, но блок не попадёт в отчёт
    if mission not in (MISSIONS) or mission not in MISSION_TO_DIALOG_TYPE:
        mission_norm = "unknown"
    else:
        mission_norm = mission

    if mission_norm == "Купить":
        if not is_sale:
            loss = loss  # оставляем допустимую причину или None
        else:
            loss = None
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
# Обработка одного блока
# ---------------------------------------------------------------------------

TAIL_ROWS = 400  # сколько реплик с конца блока использовать при повторной попытке



def _log_block(index: int, block: Block, parsed: dict) -> None:
    flag = (
        "IN "
        if (parsed["role"] == "customer" and parsed["mission"] != "unknown" and parsed["confidence"] >= CONFIDENCE_MIN)
        else "OUT"
    )
    print(
        f"[{flag}] block#{index:02d} {block.store_id[:24]} "
        f"rows={len(block.rows)} span={block.duration_sec:5.0f}s "
        f"role={parsed['role']} mission={parsed['mission']} "
        f"sale={parsed['is_sale']} loss={parsed['loss_reason']} conf={parsed['confidence']:.2f}"
    )


def _attempt_one(client, block: Block, block_text: str | None) -> dict:
    prompt = build_user_prompt(
        store_id=block.store_id,
        seller_id=block.seller_id,
        client_id_hint=block.client_id_hint,
        current_dialog_type=block.dialog_types[0] if block.dialog_types else None,
        block_text_with_times=block_text if block_text is not None else format_block_with_times(block),
    )
    out = client.chat_json(SYSTEM_PROMPT, prompt)
    parsed = extract_json(out["content"])
    if not parsed:
        raise ValueError(f"no JSON in LLM response: {out['content'][:160]!r}")
    return normalize_llm_result(parsed, block)


def analyze_block(
    client: OllamaClient,
    block: Block,
    index: int,
    confidence_min: float,
    retries: int = 2,
    verbose: bool = True,
) -> dict:
    last_error = None
    # Полный блок. Если слишком длинный — повторная попытка с урезанным хвостом.
    for attempt in range(retries + 1):
        try:
            text = format_block_with_times(block)
            parsed = _attempt_one(client, block, text)
            parsed["block_index"] = index
            if verbose:
                _log_block(index, block, parsed)
            return parsed
        except (OllamaError, ValueError) as exc:
            last_error = exc
            if verbose:
                print(f"[ERR] block#{index:02d} attempt {attempt + 1}: {exc}")
            time.sleep(5)

    # Fallback: хвост блока (последние N реплик) — там чаще всего ключевые реплики.
    if len(block.rows) > 80:
        try:
            tail_rows = block.rows[-TAIL_ROWS:]
            tail_text = "\n".join(
                f"{r.created_at:%H:%M:%S} | {' '.join((r.text or '').split())}" for r in tail_rows
            )
            parsed = _attempt_one(client, block, tail_text)
            parsed["block_index"] = index
            parsed.setdefault("reason", "")
            parsed["reason"] = (parsed.get("reason") or "") + " [оценка по хвосту блока]"
            parsed["confidence"] = min(parsed["confidence"], 0.85)
            if verbose:
                _log_block(index, block, parsed)
            return parsed
        except (OllamaError, ValueError) as exc:
            last_error = exc
            if verbose:
                print(f"[ERR] block#{index:02d} fallback-tail: {exc}")

    if verbose:
        print(f"[SKIP-MODEL] block#{index:02d} — не удалось получить результат: {last_error}")
    return {
        "block_index": index,
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
# Постобработка и запись Excel
# ---------------------------------------------------------------------------

def build_final_rows(all_results: list[dict], blocks: list[Block], confidence_min: float) -> tuple[list[dict], list[dict]]:
    """Возвращает (rows_для_отчёта, отклонённые_блоки).

    1 строка отчёта = 1 отдельный клиентский разговор (блок).
    Каждый разговор получает уникальный ключ identity (internal client_id +
    suffix при коллизии — realtime-счётчик сбрасывается при каждом рестарте
    процесса, поэтому два разных разговора могут попасть под один ID).
    display client_id — последовательный внутри магазина (assign_display_client_ids),
    internal_client_id — технический ID.
    """
    rows = []
    rejected = []
    store_seen: dict[str, dict[str, int]] = {}

    for r, block in zip(all_results, blocks):
        if not (r["role"] == "customer" and not r.get("llm_error")):
            rejected.append(
                {
                    "block_index": r["block_index"],
                    "role": r["role"],
                    "mission": r["mission"],
                    "confidence": r["confidence"],
                    "reason": r["reason"],
                    "llm_error": r.get("llm_error"),
                }
            )
            continue

        store = block.store_id
        internal_client_id = block.client_id_hint
        block_key = f"block_{r['block_index']:02d}"
        base_key = internal_client_id if internal_client_id else f"offline_{store}_{block_key}"

        seen = store_seen.setdefault(store, {})
        seen[base_key] = seen.get(base_key, 0) + 1
        identity_key = base_key if seen[base_key] == 1 else f"{base_key}#{seen[base_key]}"

        text = block.text
        session_ids = ", ".join(block.session_ids)

        start = block.start
        end = block.end
        duration = block.duration_sec
        dt = r.get("dialog_type")
        if not dt or not str(dt).strip():
            dt = MISSION_TO_DIALOG_TYPE["Прочее"]

        rows.append(
            {
                "store_id": store,
                "seller_id": block.seller_id,
                "client_id": identity_key,
                "internal_client_id": internal_client_id,
                "recognition_text": text,
                "dialog_type": dt,
                "is_sale": bool(r["is_sale"]),
                "loss_reason": r["loss_reason"],
                "is_alarm_triggered": bool(block.alarm_any),
                "dialog_start_at": start,
                "dialog_end_at": end,
                "dialog_duration_sec": round(duration, 3),
                "session_ids": session_ids,
                "model_reasoning": r.get("reason") if isinstance(r.get("reason"), str) and r.get("reason", "").strip() else None,
                "block_id": block_key,
            }
        )

    rows = assign_display_client_ids(rows)
    rows.sort(key=lambda x: (x["store_id"], x.get("dialog_start_at") or datetime.min))
    return rows, rejected


def write_excel(rows: list[dict], output_path: str) -> None:
    out = pd.DataFrame(rows)
    for c in REPORT_COLUMNS:
        if c not in out.columns:
            out[c] = None
    out = out[REPORT_COLUMNS]
    for col in ("dialog_start_at", "dialog_end_at"):
        out[col] = pd.to_datetime(out[col], errors="coerce").dt.tz_localize(None)

    out["is_sale"] = out["is_sale"].fillna(False).astype(bool)
    out["is_alarm_triggered"] = out["is_alarm_triggered"].fillna(False).astype(bool)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        out.to_excel(writer, index=False, sheet_name="report")
        ws = writer.sheets["report"]
        for row in ws.iter_rows():
            for cell in row:
                cell.alignment = cell.alignment.copy(wrap_text=True)


def write_debug(all_results: list[dict], blocks: list[Block], rejected: list[dict], debug_path: str) -> None:
    items = []
    offline_counter = 0
    for idx, (r, block) in enumerate(zip(all_results, blocks)):
        existing_client_id = block.client_id_hint
        if existing_client_id:
            final_client_id = existing_client_id
            block_id = f"block_{idx:02d}"
        else:
            # Технический block_id (допускается только внутри debug).
            offline_counter += 1
            short = re.sub(r"[^0-9A-Za-z_А-Яа-я]+", "", block.store_id)[:16]
            block_id = f"offline_{short}_{offline_counter:02d}"
            final_client_id = None  # реального client_id в блоке не было
        reasoning = r.get("reason")
        if not isinstance(reasoning, str) or not reasoning.strip():
            reasoning = None
        items.append(
            {
                "block_id": block_id,
                "store_id": block.store_id,
                "existing_client_id": existing_client_id,
                "final_client_id": final_client_id,
                "rows": len(block.rows),
                "role": r["role"],
                "mission": r["mission"],
                "dialog_type": r.get("dialog_type"),
                "is_sale": r["is_sale"],
                "loss_reason": r["loss_reason"],
                "confidence": r["confidence"],
                "reason": r["reason"],
                "model_reasoning": reasoning,
                "start_time": block.start.isoformat(),
                "end_time": block.end.isoformat(),
                "session_ids": block.session_ids,
                "recognition_text": block.text,
                "llm_error": r.get("llm_error"),
            }
        )
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_blocks": len(all_results),
        "accepted_customers": sum(1 for it in items if it["role"] == "customer"),
        "rejected_blocks": len(rejected),
        "blocks": items,
    }
    Path(debug_path).parent.mkdir(parents=True, exist_ok=True)
    with open(debug_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def print_stats(
    total_rows: int,
    blocks: list[Block],
    all_results: list[dict],
    final_rows: list[dict],
    input_file: str,
    output_file: str,
) -> None:
    role_counts = {}
    for r in all_results:
        role_counts[r["role"]] = role_counts.get(r["role"], 0) + 1

    customers = [r for r in all_results if r["role"] == "customer"]
    buy = [r for r in customers if r["mission"] == "Купить"]
    loss_counts: dict[str | None, int] = {}
    for r in buy:
        if not r["is_sale"]:
            key = r["loss_reason"] or "(null)"
            loss_counts[key] = loss_counts.get(key, 0) + 1

    dt_counts = {}
    for r in final_rows:
        dt_counts[r["dialog_type"]] = dt_counts.get(r["dialog_type"], 0) + 1

    print("\n" + "=" * 70)
    print("СТАТИСТИКА OFFLINE-АНАЛИЗА")
    print("=" * 70)
    print(f"Исходный файл            : {input_file}")
    print(f"Финальный отчёт          : {output_file}")
    print(f"ASR-записей (строк)      : {total_rows}")
    print(f"Сформировано блоков      : {len(blocks)}")
    print(f"  role=customer          : {role_counts.get('customer', 0)}")
    print(f"  role=employee          : {role_counts.get('employee', 0)}")
    print(f"  role=background        : {role_counts.get('background', 0)}")
    print(f"  role=unknown           : {role_counts.get('unknown', 0)}")
    print(f"customer с миссией        : {sum(1 for r in customers if r['mission'] != 'unknown')}")
    print(f"customer, mission=Купить  : {len(buy)}")
    print(f"  buy, is_sale=true       : {sum(1 for r in buy if r['is_sale'])}")
    print(f"  buy, is_sale=false      : {sum(1 for r in buy if not r['is_sale'])}")
    print("  loss_reason по buy-без-покупки:")
    if loss_counts:
        for k, v in sorted(loss_counts.items(), key=lambda x: -x[1]):
            print(f"    - {k}: {v}")
    else:
        print("    (нет)")
    print("dialog_type в финальном отчёте:")
    for k, v in sorted(dt_counts.items(), key=lambda x: str(x[0])):
        print(f"    - {k}: {v}")
    print(f"Строк в финальном отчёте: {len(final_rows)}")
    print("=" * 70)


def show_examples(all_results: list[dict], blocks: list[Block]) -> None:
    def fmt_block(block: Block, max_chars: int = 600) -> str:
        text = block.text
        if len(text) > max_chars:
            half = max_chars // 2
            text = text[:half] + "\n... [сокращено] ...\n" + text[-half:]
        return text

    def find(role=None, mission=None, sale=None, not_mission=None):
        out = []
        for r, b in zip(all_results, blocks):
            if role is not None and r["role"] != role:
                continue
            if mission is not None and r["mission"] != mission:
                continue
            if sale is not None and bool(r["is_sale"]) is not bool(sale):
                continue
            if not_mission is not None and r["mission"] == not_mission:
                continue
            out.append((r, b))
        return out

    print("\n" + "=" * 70)
    print("ПРИМЕРЫ")
    print("=" * 70)

    def dump(title: str, r, b, max_chars=700):
        print("\n" + "-" * 70)
        print(f"{title}")
        print(f"  store={b.store_id} client_hint={b.client_id_hint}")
        print(f"  time={b.start:%H:%M:%S} -> {b.end:%H:%M:%S} ({b.duration_sec:.0f}s)")
        print(f"  LLM: role={r['role']} mission={r['mission']} is_sale={r['is_sale']} "
              f"loss={r['loss_reason']} conf={r['confidence']:.2f}")
        print(f"  reason: {r.get('reason')}")
        print("  --- Текст (фрагмент) ---")
        for line in fmt_block(b, max_chars).splitlines():
            print("   |", line)

    examples = [
        ("1) Клиент, покупка состоялась", find(role="customer", mission="Купить", sale=True) or None),
        ("2) Клиент, покупka НЕ состоялась", find(role="customer", mission="Купить", sale=False) or None),
        ("3) Сотрудников разговор (отклонён)", find(role="employee")[0] if find(role="employee") else None),
        ("4) Background (отклонён)", find(role="background")[0] if find(role="background") else None),
        ("5) Неоднозначный случай (customer с низкой уверенностью / миссия unknown)",
         sorted([(r, b) for r, b in zip(all_results, blocks) if r["role"] in ("customer", "unknown")],
                key=lambda x: x[0]["confidence"])[:1] or None),
    ]
    for title, candidate in examples:
        if candidate is None or (isinstance(candidate, tuple) and candidate == ()):
            print(f"\n{title}: не найдено")
            continue
        if isinstance(candidate, list):
            candidate = candidate[0]
        r, b = candidate
        dump(title, r, b)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def run(args) -> int:
    input_file = args.input
    output_file = args.output or input_file_path_to_final(input_file)
    debug_path = args.debug or Path(output_file).with_name(Path(output_file).stem + "_debug.json")

    print(f"OFFLINE анализ: {input_file}")
    sheets = load_raw(input_file)
    total_rows = 0
    store_rows = []
    for store_id, df in sheets:
        total_rows += len(df)
        store_rows.append((store_id, df_to_rows(df)))
    print(f"ASR-записей: {total_rows} (магазинов: {len(store_rows)})")

    # 1) segmentation
    blocks: list[Block] = []
    for store_id, rows in store_rows:
        blocks.extend(build_blocks(rows, store_id))
    print(f"Сегментация: {len(blocks)} блоков-кандидатов")

    # 2) LLM
    client = OllamaClient(model=args.model)
    print(f"LLM: {client.model} @ {client.host}")

    results: list[dict] = [None] * len(blocks)
    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {
                pool.submit(analyze_block, client, blocks[i], i, args.confidence_min, args.retries, not args.quiet): i
                for i in range(len(blocks))
            }
            for fut in as_completed(futs):
                i = futs[fut]
                results[i] = fut.result()
    else:
        for i in range(len(blocks)):
            results[i] = analyze_block(client, blocks[i], i, args.confidence_min, args.retries, not args.quiet)

    # 3) финальный отчёт
    final_rows, rejected = build_final_rows(results, blocks, args.confidence_min)
    write_excel(final_rows, output_file)
    write_debug(results, blocks, rejected, debug_path)
    if args.debug_dir:
        Path(args.debug_dir).mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy(debug_path, str(Path(args.debug_dir) / Path(debug_path).name))

    # 4) статистика + примеры
    print_stats(total_rows, blocks, results, final_rows, input_file, output_file)
    if not args.no_examples:
        show_examples(results, blocks)
    print("\nDONE")
    return 0


def input_file_path_to_final(input_file: str) -> str:
    p = Path(input_file)
    m = re.search(r"(\d{4}-\d{2}-\d{2})", p.stem)
    date_part = m.group(1) if m else time.strftime("%Y-%m-%d")
    return str(p.parent / f"final_transcript_report_{date_part}.xlsx")


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline-анализ дневных транскрипций + финальный Excel-отчёт.")
    parser.add_argument("--input", default=None, help="сырой дневной Excel с транскрипциями")
    parser.add_argument("--output", default=None, help="путь к финальному Excel (по умолчанию: reports/final_transcript_report_<дата>.xlsx)")
    parser.add_argument("--model", default="qwen3.8:27b", help="модель Ollama")
    parser.add_argument("--gap-sec", type=float, default=180.0, help="порог паузы (сек) для разделения под-блоков")
    parser.add_argument("--max-sec", type=float, default=1200.0, help="максимальная длительность одного блока (сек)")
    parser.add_argument("--confidence-min", type=float, default=CONFIDENCE_MIN, help="минимальная уверенность LLM")
    parser.add_argument("--workers", type=int, default=1, help="параллельные запросы к LLM")
    parser.add_argument("--retries", type=int, default=2, help="повторы запроса к LLM")
    parser.add_argument("--debug", default=None, help="путь к debug JSON")
    parser.add_argument("--debug-dir", default=None, help="доп. каталог для debug-копий")
    parser.add_argument("--quiet", action="store_true", help="не печатать построчно")
    parser.add_argument("--no-examples", action="store_true", help="не показывать примеры")
    args = parser.parse_args()

    if not args.input:
        default = Path(__file__).resolve().parents[1] / "reports"
        candidates = sorted((default / "transcript_report_latest.xlsx" for _ in [0])) if (default / "transcript_report_latest.xlsx").exists() else None
        if candidates:
            args.input = str(candidates[0])
        else:
            # берём последний transcript_report_*.xlsx
            files = sorted(default.glob("transcript_report_*.xlsx")) if default.exists() else []
            files = [f for f in files if not f.name.startswith("final_")]
            if not files:
                parser.error("--input не задан и не найден автоматический файл")
            args.input = str(files[-1])

    return run(args)


if __name__ == "__main__":
    sys.exit(main())
