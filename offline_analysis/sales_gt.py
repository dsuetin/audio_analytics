"""Sales Ground-Truth — EXPERIMENT mode ("--sales-ground-truth <path>").

Цель: использовать реальные документы продаж как ДОПОЛНИТЕЛЬНОЕ ограничение
сегментации/классификации (НЕ как источник истины для всех диалогов), и
сравнить найденные customer-диалоги с реальными продажами.

Экспериментальный модуль — не используется в production-конвейере, пока не
указан флаг ``--sales-ground-truth``. Без флага pipeline работает как раньше.

Ключевые сущности:
  * ``SalesRecord``            — одна строка документа продаж (магазин + время + check id).
  * ``load_sales_ground_truth``— читает Excel документа продаж.
  * ``match_store_to_store_id``— сопоставляет название магазина из документа
                                 с ``store_id`` транскрипции (без ручного
                                 hardcode: эвристика токенов + difflib).
  * ``make_sales_snippet``     — prompt-фрагмент для LLM (магазин/время/id продажи).
  * ``match_sales_to_dialogs`` — сопоставляет продажи с final-диалогами
                                 (статусы MATCHED / AMBIGUOUS / WRONG_TYPE / NOT_FOUND).
  * ``sales_metrics``          — recall / precision / F1.
  * ``write_matching_xlsx``    — пишет report + false-positive лист.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd


# ---------------------------------------------------------------------------
# Хранение
# ---------------------------------------------------------------------------

@dataclass
class SalesRecord:
    """Одна запись документа продаж."""

    id: int                       # 0-based порядковый номер записи (стабильный)
    store_raw: str                # название магазина из документа (как есть)
    sale_time: datetime           # дата/время покупки (YYYY-MM-DD HH:MM:SS)
    doc_raw: str                  # строка "Документ" (чек ККМ ...)
    doc_id: str                   # id чека (УТ000249358) если извлекается
    op_kind: str                  # "продажа" | "чек на возврат" | ...
    store_id: str | None = None   # сопоставленный store_id транскрипции

    @property
    def is_purchase(self) -> bool:
        """True для фактических покупок (исключая чеки на возврат)."""
        return "возврат" not in (self.op_kind or "").lower()


@dataclass
class MatchResult:
    """Результат сопоставления одной продажи с final-диалогами."""

    sale: SalesRecord
    store_id: str | None = None     # store_id транскрипции, к которой отнесена продажа
    status: str = "NOT_FOUND"      # MATCHED | AMBIGUOUS | WRONG_TYPE | NOT_FOUND
    matched_dialog: str | None = None   # client_id matched-диалога
    dialog_start: datetime | None = None
    dialog_end: datetime | None = None
    dialog_type: str | None = None
    dialog_is_sale: bool | None = None
    n_candidate_dialogs: int = 0
    n_sales_dialogs: int = 0       # candidate-диалоги, помеченные как buy/is_sale
    best_gap_sec: int | None = None  # расстояние до ближайшего candidate-диалога
    reason: str = ""
    candidates: list[dict] = field(default_factory=list)  # короткие описания соседних
    ambiguity: list[dict] = field(default_factory=list)   # sales-диалоги-кандидаты


# ---------------------------------------------------------------------------
# Сопоставление названия магазина
# ---------------------------------------------------------------------------

# Словесные токены, НЕ несущие смысловой нагрузки для区分ации конкретной
# точки (город / часть адреса "ул." / "д." / "ш." / одиночные цифры) —
# убираем из сравнения, чтобы "г.Минеральные Воды" не мешал совпадению.
_STOP_TOKENS = {
    "г", "город", "пятигорск", "минеральные", "воды", "ул", "улица",
    "просп", "проспект", "ш", "шоссе", "д", "б", "а", "к",
}


def _norm_tokens(name: str) -> list[str]:
    s = str(name).lower()
    s = re.sub(r"[^a-zа-яё0-9]+", " ", s)
    return [t for t in s.split() if t and t not in _STOP_TOKENS]


def _common_substring_len(a: str, b: str) -> int:
    """Длина наибольшего общего подстрока-блока (для устойчивости к обрезкам)."""
    a = re.sub(r"[^a-zа-яё0-9]", " ", str(a).lower())
    b = re.sub(r"[^a-zа-яё0-9]", " ", str(b).lower())
    best = 0
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            if k > best:
                best = k
    return best


def match_store_to_store_id(store_name: str, store_ids: Iterable[str]) -> tuple[str, float]:
    """Сопоставляет название магазина с ближайшим ``store_id`` транскрипции.

    Эвристика: совпадение смысловых токенов + длина общей подстроки. Возвращает
    ``(store_id, score)``. Если ни одно имя не совпадает достаточно хорошо —
    возвращаем лучший по счёту (не None), решаем ли использовать — выше на
    уровне вызывающего кода.
    """
    candidates = [s for s in store_ids if s and s.lower() != "main_store"]
    best, best_score = None, -1.0
    name_tokens = set(_norm_tokens(store_name))
    for sid in candidates:
        sid_tokens = set(_norm_tokens(sid))
        # токены совпало
        overlap = len(name_tokens & sid_tokens)
        # общие блоки (устойчивость к "Головин д. 29" vs "ул. Калинина, 299")
        sub = _common_substring_len(store_name, sid)
        # score: 10 за общий подстрок + 3 за каждый общий токен
        score = sub * 10 + overlap * 3
        if score > best_score:
            best_score = score
            best = sid
    # difflib как второй взгляд на "название похожее на trunc store_id"
    for sid in candidates:
        ratio = difflib.SequenceMatcher(None, store_name.lower(), sid.lower()).ratio()
        score = ratio * 4 + 10  # small baseline
        if ratio > 0.35:
            score += (ratio * 30)
        if score > best_score:
            best_score = score
            best = sid
    return best, best_score


# ---------------------------------------------------------------------------
# Загрузка ground-truth
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})\s*$")
_DOC_ID_RE = re.compile(r"(УТ\d+|[A-ZА-Я]{2,}\d{5,})", re.IGNORECASE)


def _parse_sale_time(value: object, day: datetime) -> datetime | None:
    """Принимает время из "DD.MM.YYYY HH:MM:SS" или "HH:MM:SS".

    При отсутствии даты — используем переданный ``day`` (день эксперимента).
    """
    s = str(value).strip()
    m = _TIME_RE.search(s)
    if not m:
        return None
    h, mi, sec = int(m.group(1)), int(m.group(2)), int(m.group(3))
    # дата
    date_part: dtime | None = None
    if " " in s:
        head = s.split(" ", 1)[0]
        try:
            date_part = datetime.strptime(head, "%d.%m.%Y").date()
        except ValueError:
            date_part = day.date()
    if date_part is None:
        date_part = day.date()
    try:
        return datetime.combine(date_part, dtime(h, mi, sec))
    except ValueError:
        return None


def load_sales_ground_truth(
    xlsx_path: str | Path,
    day: datetime | None = None,
    extra_store_ids: Sequence[str] = (),
) -> tuple[list[SalesRecord], dict]:
    """Считывает Excel документа продаж -> список ``SalesRecord``.

    Возвращает ``(records, meta)``:
      * ``records`` — строки в порядке файла (без пропуска "возвратной").
      * ``meta``    — служебная информация (названия листов, количество,
                      mapping-кандидаты если ``extra_store_ids`` задан).
    """
    xlsx_path = str(xlsx_path)
    if not Path(xlsx_path).exists():
        raise FileNotFoundError(f"sales ground-truth file not found: {xlsx_path}")

    df = pd.read_excel(xlsx_path, sheet_name=0)
    # нормализуем имена колонок (пробелы / кириллица ок)
    colmap = {}
    for c in df.columns:
        colmap[str(c).strip().lower()] = c
    store_col = colmap.get("магазин")
    date_col = colmap.get("дата")
    doc_col = colmap.get("документ")
    op_col = colmap.get("видоперации") or colmap.get("вид операции") or colmap.get("op_kind")

    if not (store_col and date_col and op_col):
        raise ValueError(
            f"sales ground-truth file: required columns not found. "
            f"Got: {list(df.columns)}; need columns 'Магазин','Дата','Документ','ВидОперации'"
        )

    # day по умолчанию — сегодня (запускать в день анализа); можно передать явно.
    if day is None:
        day = datetime.now()

    records: list[SalesRecord] = []
    for i, rec in df.iterrows():
        store_raw = str(rec[store_col] or "").strip()
        sale_time = _parse_sale_time(rec[date_col], day)
        doc_raw = str(rec[doc_col] or "").strip()
        op_kind = str(rec[op_col] or "").strip()
        if not store_raw or sale_time is None:
            # пропускаем строки без минимальных данных (но фиксируем)
            continue
        m = _DOC_ID_RE.search(doc_raw)
        records.append(SalesRecord(
            id=len(records),
            store_raw=store_raw,
            sale_time=sale_time,
            doc_raw=doc_raw,
            doc_id=(m.group(1) if m else str(i)),
            op_kind=op_kind,
        ))

    meta = {
        "source": xlsx_path,
        "n_records": len(records),
        "n_purchase": sum(1 for r in records if r.is_purchase),
        "n_return": sum(1 for r in records if not r.is_purchase),
        "stores_sorted": sorted({r.store_raw for r in records}),
        "day": day.isoformat(),
        "extra_store_ids": list(extra_store_ids) if extra_store_ids else None,
    }
    return records, meta


def assign_store_ids(records: list[SalesRecord], store_ids: Iterable[str]) -> list[SalesRecord]:
    """Привязывает каждой записи ближайший ``store_id`` транскрипции (best match).

    Мутирует ``store_id`` у переданных записей. Если ``store_ids`` пустой —
    store_id остаётся None (в этом случае matching не фильтрует по магазину).
    """
    sids = [s for s in store_ids if s and s.lower() != "main_store"]
    for r in records:
        if sids:
            sid, _score = match_store_to_store_id(r.store_raw, sids)
            r.store_id = sid
        else:
            r.store_id = None
    return records


# ---------------------------------------------------------------------------
# Prompt-фрагменты (для injection в segmentation / classification)
# ---------------------------------------------------------------------------

def _fmt_hms(dt: datetime | None) -> str:
    return dt.strftime("%H:%M:%S") if dt is not None else "-"


def make_sales_snippet(
    records: Sequence[SalesRecord],
    store_id: str,
    max_records: int | None = 20,
) -> str:
    """Собирает компактный фрагмент prompt'а: реальные продажи этого магазина.

    НЕ говорит модели "поставь buy" — только сообщает, что существует продажа в
    это время, и просит её найти как отдельный dialog.
    """
    if not records:
        return ""
    # sort by time
    ordered = sorted(records, key=lambda r: r.sale_time)
    if max_records and len(ordered) > max_records:
        ordered = ordered[:max_records]
    lines = [
        f"  SALE[{r.id:03d}] {r.sale_time.strftime('%H:%M:%S')} "
        f"check={r.doc_id} kind={r.op_kind or '-'}"
        for r in ordered
    ]
    block = (
        "\n\nДОПОЛНИТЕЛЬНОЕ ОГРАНИЧЕНИЕ — реальные продажи этого магазина (из документа ККМ):\n"
        "  Каждая запись означает: вокруг этого времени должен быть один отдельный "
        "customer-диалог с завершённой покупкой (dialog_type=buy / is_sale=true).\n"
        "  Эти записи НЕ являются полным списком диалогов — вокруг и между ними могут "
        "быть service / help / other-диалоги, которые тоже нужно сохранить.\n"
        "  Если рядом находится несколько близких продаж (2-3 за 10-30 минут) — это "
        "несколько РАЗНЫХ клиентов: каждый SALE → отдельный dialog.\n\n"
        "Реальные продажи:\n"
        + "\n".join(lines)
        + "\n"
    )
    return block


# ---------------------------------------------------------------------------
# Matching (продажи <-> final-диалоги)
# ---------------------------------------------------------------------------

def _sod(dt: datetime) -> int:
    return dt.hour * 3600 + dt.minute * 60 + dt.second


def match_sales_to_dialogs(
    records: Sequence[SalesRecord],
    finals: list[dict],
    core_window_sec: int = 300,
    candidate_window_sec: int = 600,
    strict_store: bool = True,
) -> list[MatchResult]:
    """Сопоставляет продажи с final-диалогами.

    Правила:
      * candidate-диалог — тот же store (если strict_store=True) И
        ``sale_time`` внутри ``[start - candidate_window, end + candidate_window]``.
      * sales-диалог (matching candidate) — candidate + (dialog_type=='buy' OR is_sale).
      * MATCHED  — ровно 1 sales-диалог среди candidates;
      * AMBIGUOUS — >=2 sales-диалога;
      * WRONG_TYPE — есть candidate(-ы) но ни одного sales-диалога;
      * NOT_FOUND — нет ни одного candidate-диалога.

    ``finals`` — список dict с ключами: client_id, store_id, dialog_type,
    is_sale, dialog_start_at, dialog_end_at, recognition_text, model_reasoning.
    """
    # индекс: store_id -> список (start_s, end_s, row)
    index: dict[str, list[tuple[int, int, dict]]] = {}
    for row in finals:
        sid = row.get("store_id") or "main_store"
        st = row.get("dialog_start_at")
        en = row.get("dialog_end_at")
        if st is None or en is None:
            continue
        index.setdefault(sid, []).append((_sod(st), _sod(en), row))

    results: list[MatchResult] = []
    for r in records:
        # store_id записи (привязанный в assign_store_ids)
        sid = r.store_id
        if strict_store and sid:
            pool_sids = [sid]
        else:
            pool_sids = list(index.keys())

        ss = _sod(r.sale_time)
        candidates: list[tuple[int, int, dict]] = []
        for ps in pool_sids:
            for (st, en, row) in index.get(ps, []):
                # candidate: sale inside [start - cw, end + cw]
                if (st - candidate_window_sec) <= ss <= (en + candidate_window_sec):
                    candidates.append((st, en, row))
        if not candidates:
            results.append(MatchResult(
                sale=r, store_id=sid, status="NOT_FOUND",
                n_candidate_dialogs=0, n_sales_dialogs=0,
                reason=f"no dialog within ±{candidate_window_sec}s at store {sid}"
                       if sid else "no dialog (store mapping failed or no dialogs)",
            ))
            continue

        # candidates с расчётом расстояния до span
        def gap(st, en, ss):
            if st <= ss <= en:
                return 0
            return abs((st + en) // 2 - ss)
        scored = sorted(
            [(gap(st, en, ss), st, en, row) for (st, en, row) in candidates],
            key=lambda x: (x[0], x[1]),  # min gap first, then earliest start
        )
        sales_dlg = [
            (g, st, en, row) for (g, st, en, row) in scored
            if (row.get("is_sale") is True or row.get("dialog_type") == "buy")
        ]
        if len(sales_dlg) == 1:
            g, st, en, row = sales_dlg[0]
            results.append(MatchResult(
                sale=r, store_id=sid, status="MATCHED",
                matched_dialog=row.get("client_id"),
                dialog_start=row.get("dialog_start_at"),
                dialog_end=row.get("dialog_end_at"),
                dialog_type=row.get("dialog_type"),
                dialog_is_sale=row.get("is_sale"),
                n_candidate_dialogs=len(scored),
                n_sales_dialogs=1,
                best_gap_sec=g,
                reason="exactly one sales dialog in window",
                candidates=[_short(row, g) for g, st, en, row in scored[:8]],
            ))
        elif len(sales_dlg) >= 2:
            # AMBIGUOUS — берём ближайший для отчёта
            g, st, en, row = sales_dlg[0]
            results.append(MatchResult(
                sale=r, store_id=sid, status="AMBIGUOUS",
                matched_dialog=row.get("client_id"),
                dialog_start=row.get("dialog_start_at"),
                dialog_end=row.get("dialog_end_at"),
                dialog_type=row.get("dialog_type"),
                dialog_is_sale=row.get("is_sale"),
                n_candidate_dialogs=len(scored),
                n_sales_dialogs=len(sales_dlg),
                best_gap_sec=g,
                reason=f"{len(sales_dlg)} sales dialogs in window — ambiguous",
                candidates=[_short(row, g) for g, st, en, row in sales_dlg[:8]],
            ))
        else:
            g, st, en, row = scored[0]
            results.append(MatchResult(
                sale=r, store_id=sid, status="WRONG_TYPE",
                matched_dialog=row.get("client_id"),
                dialog_start=row.get("dialog_start_at"),
                dialog_end=row.get("dialog_end_at"),
                dialog_type=row.get("dialog_type"),
                dialog_is_sale=row.get("is_sale"),
                n_candidate_dialogs=len(scored),
                n_sales_dialogs=0,
                best_gap_sec=g,
                reason=f"candidates {len(scored)} but none is buy; nearest={row.get('dialog_type')}",
                candidates=[_short(row, g) for g, st, en, row in scored[:8]],
            ))
    return results


def _short(row: dict, gap: int) -> dict:
    return {
        "client_id": row.get("client_id"),
        "dialog_type": row.get("dialog_type"),
        "is_sale": bool(row.get("is_sale")),
        "start": _fmt_hms(row.get("dialog_start_at")),
        "end": _fmt_hms(row.get("dialog_end_at")),
        "gap_sec": int(gap),
        "reason": (row.get("model_reasoning") or "")[:120],
    }


def find_false_positive_buys(results: list[MatchResult], finals: list[dict]) -> list[dict]:
    """Все buy-диалоги, которым не соответствует ни одна продажа."""
    claimed = {
        (res.store_id, res.matched_dialog)
        for res in results
        if res.matched_dialog is not None and res.status in ("MATCHED", "AMBIGUOUS")
    }
    out: list[dict] = []
    for row in finals:
        if row.get("dialog_type") == "buy" or row.get("is_sale") is True:
            key = (row.get("store_id"), row.get("client_id"))
            if key not in claimed:
                out.append({
                    "client_id": row.get("client_id"),
                    "store_id": row.get("store_id"),
                    "dialog_type": row.get("dialog_type"),
                    "is_sale": row.get("is_sale"),
                    "start": _fmt_hms(row.get("dialog_start_at")),
                    "end": _fmt_hms(row.get("dialog_end_at")),
                    "reason": (row.get("model_reasoning") or "")[:250],
                    "n_sales_rows": _count_sales_rows_near(row, results),
                })
    return out


def _count_sales_rows_near(row: dict, results: list[MatchResult]) -> int:
    """Сколько sales-записей имеют this row как 'best' (даже если WRONG_TYPE)."""
    cnt = 0
    for res in results:
        if res.matched_dialog == row.get("client_id") and res.store_id == row.get("store_id"):
            cnt += 1
    return cnt


def multi_sale_dialogs(results: list[MatchResult]) -> list[dict]:
    """Диалоги, к которым привязано >=2 SALE (сигнал «слишком длинный / несколько покупок»)."""
    from collections import defaultdict
    d: dict = defaultdict(int)
    sales_for = defaultdict(list)
    for res in results:
        if res.matched_dialog is None:
            continue
        if res.status not in ("MATCHED", "AMBIGUOUS"):
            continue
        key = (res.store_id, res.matched_dialog)
        d[key] += 1
        sales_for[key].append(res.sale.id)
    return [
        {"store_id": k[0], "client_id": k[1], "n_sales": v, "sale_ids": sales_for[k]}
        for k, v in d.items() if v >= 2
    ]


# ---------------------------------------------------------------------------
# Метрики
# ---------------------------------------------------------------------------

def sales_metrics(results: list[MatchResult], finals: list[dict]) -> dict:
    total_sales = len([r for r in results if r.sale.is_purchase])
    matched = sum(1 for r in results if r.sale.is_purchase and r.status == "MATCHED")
    ambiguous = sum(1 for r in results if r.sale.is_purchase and r.status == "AMBIGUOUS")
    wrong_type = sum(1 for r in results if r.sale.is_purchase and r.status == "WRONG_TYPE")
    not_found = sum(1 for r in results if r.sale.is_purchase and r.status == "NOT_FOUND")

    # buys — все final-диалоги, помеченные как buy / is_sale
    buys = [f for f in finals if f.get("dialog_type") == "buy" or f.get("is_sale") is True]
    n_buys = len(buys)
    n_fp = len(find_false_positive_buys(results, finals))
    # matched-продажи, которые реально привязаны к buy-dialog (MATCHED/AMBIGUOUS)
    n_match_sales = sum(
        1 for r in results
        if r.sale.is_purchase and r.status in ("MATCHED", "AMBIGUOUS")
    )

    recall = (matched + ambiguous) / total_sales if total_sales else 0.0
    # Strict Recall = только MATCHED
    strict_recall = matched / total_sales if total_sales else 0.0
    # Precision = matched sales / detected buys
    precision = (n_match_sales / n_buys) if n_buys else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "total_sales": total_sales,
        "matched": matched,
        "ambiguous": ambiguous,
        "wrong_type": wrong_type,
        "not_found": not_found,
        "total_detected_buys": n_buys,
        "false_positive_buys": n_fp,
        "strict_recall": round(strict_recall, 4),
        "recall_with_ambiguous": round(recall, 4),
        "precision": round(precision, 4),
        "f1_with_ambiguous": round(f1, 4),
        "n_multi_sale_dialogs": len(multi_sale_dialogs(results)),
    }


# ---------------------------------------------------------------------------
# Writing — sales_matching_<date>.xlsx
# ---------------------------------------------------------------------------

def write_matching_xlsx(
    results: list[MatchResult],
    finals: list[dict],
    output_path: str,
    meta: dict | None = None,
    metrics: dict | None = None,
) -> None:
    """Пишет detailed matching report + false positives + метрики."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # sheet 1: Matching — одна строка на каждую продажу
    matching_rows = []
    for res in results:
        matching_rows.append({
            "sale_id": res.sale.id,
            "store_raw": res.sale.store_raw,
            "store_id": res.store_id or res.sale.store_raw,
            "sale_time": res.sale.sale_time,
            "doc_id": res.sale.doc_id,
            "op_kind": res.sale.op_kind,
            "is_purchase": res.sale.is_purchase,
            "matched_dialog": res.matched_dialog or None,
            "dialog_start": res.dialog_start,
            "dialog_end": res.dialog_end,
            "dialog_type": res.dialog_type,
            "is_sale": res.dialog_is_sale,
            "n_candidates": res.n_candidate_dialogs,
            "n_sales_in_window": res.n_sales_dialogs,
            "best_gap_sec": res.best_gap_sec,
            "match_status": res.status,
            "reason": res.reason,
            "candidates_json": _dump(res.candidates),
        })
    matching_df = pd.DataFrame(matching_rows)

    # sheet 2: False positives — buys без привязанной продажи
    fp_rows = find_false_positive_buys(results, finals)
    fp_df = pd.DataFrame(fp_rows) if fp_rows else pd.DataFrame(
        columns=["client_id", "store_id", "dialog_type", "is_sale",
                 "start", "end", "reason", "n_sales_rows"])

    # sheet 3: Multi-sale dialogs (>=2 sales -> 1 dialog)
    multi = multi_sale_dialogs(results)
    multi_df = pd.DataFrame(multi) if multi else pd.DataFrame(
        columns=["store_id", "client_id", "n_sales", "sale_ids"])

    # sheet 4: Metrics
    metrics_df = pd.DataFrame([(k, v) for k, v in (metrics or {}).items()],
                              columns=["metric", "value"])
    if meta:
        metrics_df = pd.concat([
            metrics_df,
            pd.DataFrame([(f"meta.{k}", v) for k, v in meta.items() if not isinstance(v, list)],
                         columns=["metric", "value"]),
        ], ignore_index=True)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        matching_df.to_excel(writer, index=False, sheet_name="Matching")
        fp_df.to_excel(writer, index=False, sheet_name="FalsePositives")
        multi_df.to_excel(writer, index=False, sheet_name="MultiSaleDialogs")
        metrics_df.to_excel(writer, index=False, sheet_name="Metrics")
        # wrap
        for ws in writer.sheets.values():
            for row in ws.iter_rows():
                for cell in row:
                    cell.alignment = cell.alignment.copy(wrap_text=True)


def _dump(obj) -> str:
    import json
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return str(obj)
