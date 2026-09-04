"""Offline segmentation.

Цель: восстановить структуру одного дневного потока ASR-транскрипций
(«один магазин / один микрофон за день») БЕЗ опоры на существующие
``client_id`` из realtime-контура. Единственный источник правды для
segmentation — сам поток реплик с временными метками.

Ключевые инварианты модуля:
    1. ``client_id`` из исходного файла НЕ используется для сегментации,
       объединения, разделения или подсчёта клиентов. Он сохраняется лишь
       рядом с каждой строкой (``Row.client_id``) для debug/sравнения.
    2. Итоговые сегменты покрывают ВСЕ исходные строки, без пересечений
       и без пропусков (union сегментов == все строки).
    3. Если целый дневной поток не помещается в контекст LLM — поток
       делится на чанки (на границах реплик), каждый сегментируется, затем
       на стыках чанков проверяется, не является ли граница ложной,
       и результат собирается в единый список сегментов дня.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Protocol, Sequence

import pandas as pd

from .taxonomy import SEGMENT_TYPES


# ---------------------------------------------------------------------------
# Источники данных
# ---------------------------------------------------------------------------

@dataclass
class Row:
    """Одна ASR-реплика из исходного Excel (источник истины)."""

    created_at: datetime
    text: str
    seller_id: str | None
    client_id: str | None          # старый realtime ID — только для debug, НЕ для segment
    dialog_type: str | None
    is_sale: bool
    is_alarm_triggered: bool
    session_id: str | None
    index: int                    # глобальный 0-based индекс строки в потоке дня


@dataclass
class Segment:
    """Один восстановленный сегмент дня (реальный разговор/событие/фон).

    Сегмент ссылается на исходные строки (``rows`` / ``row_indices``) и НЕ
    переписывает текст — инвариант: union строк всех сегментов == все строки дня.
    """

    segment_id: str
    store_id: str
    start_index: int                      # 0-based index первой строки
    end_index: int                        # 0-based index последней строки (включительно)
    segment_type: str | None              # dialog | employee | background | unknown
    confidence: float
    reason: str | None
    rows: list[Row] = field(default_factory=list)

    @property
    def row_indices(self) -> list[int]:
        return [r.index for r in self.rows]

    @property
    def start_at(self) -> datetime | None:
        return self.rows[0].created_at if self.rows else None

    @property
    def end_at(self) -> datetime | None:
        return self.rows[-1].created_at if self.rows else None

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def duration_sec(self) -> float:
        if self.start_at and self.end_at:
            return float((self.end_at - self.start_at).total_seconds())
        return 0.0

    @property
    def text(self) -> str:
        return "\n".join(r.text.strip() for r in self.rows if r.text and r.text.strip())

    @property
    def session_ids(self) -> list[str]:
        result: list[str] = []
        seen = set()
        for r in self.rows:
            if r.session_id and r.session_id not in seen:
                seen.add(r.session_id)
                result.append(r.session_id)
        return result

    @property
    def alarm_any(self) -> bool:
        return any(r.is_alarm_triggered for r in self.rows)

    @property
    def seller_id(self) -> str | None:
        for r in self.rows:
            if r.seller_id:
                return r.seller_id
        return None

    @property
    def old_client_ids(self) -> list[str]:
        """Уникальные старые client_id из исходного файла, встречающиеся в сегменте.

        ТОЛЬКО для debug/sравнения старой realtime-сегментации с новой.
        """
        result: list[str] = []
        for r in self.rows:
            if r.client_id and r.client_id not in result:
                result.append(r.client_id)
        return result


# ---------------------------------------------------------------------------
# Загрузка сырого файла
# ---------------------------------------------------------------------------

def load_raw(xlsx_path: str) -> list[tuple[str, pd.DataFrame]]:
    """Считывает сырой дневной файл: [(store_id, df)], df отсортирован по времени.

    Файлы-заглушки (пустой workbook без листов) -> [].
    """
    try:
        sheets = pd.read_excel(xlsx_path, sheet_name=None)
    except ValueError:
        return []
    result = []
    for store_id, df in sheets.items():
        if df is None or len(df) == 0:
            continue
        df = df.copy()
        if "created_at" in df.columns:
            df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
        df = df.dropna(subset=["created_at"])
        df = df.sort_values("created_at", kind="stable").reset_index(drop=True)
        result.append((store_id, df))
    return result


def df_to_rows(df: pd.DataFrame) -> list[Row]:
    def _s(v) -> str | None:
        if v is None:
            return None
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            pass
        s = str(v).strip()
        return s if s and s.lower() != "nan" else None

    def _b(v) -> bool:
        if isinstance(v, bool):
            return v
        s = _s(v)
        return s is not None and s.lower() in ("true", "1", "yes", "y", "t")

    rows: list[Row] = []
    for i, rec in enumerate(df.itertuples(index=False)):
        d = dict(zip(
            ["created_at", "recognition_text", "seller_id", "client_id",
             "dialog_type", "is_sale", "is_alarm_triggered", "session_id"],
            rec,
        ))
        created = pd.Timestamp(d["created_at"])
        if created is None or pd.isna(created):
            continue
        rows.append(
            Row(
                created_at=created.to_pydatetime(),
                text=_s(d["recognition_text"]) or "",
                seller_id=_s(d["seller_id"]),
                client_id=_s(d["client_id"]),
                dialog_type=_s(d["dialog_type"]),
                is_sale=_b(d["is_sale"]),
                is_alarm_triggered=_b(d["is_alarm_triggered"]),
                session_id=_s(d["session_id"]),
                index=i,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Измерение объёма (требование №6): rows / characters / estimated tokens
# ---------------------------------------------------------------------------

def estimate_row_tokens(row: Row) -> int:
    """Оценка токенов одной реплики для prompt-формата ``idx | HH:MM:SS | text``.

    Используется консервативная оценка: русскому тексту ~2.4 символа/токен,
    числом/знакам-разделителями + временная метка добавляем буфер.
    Это намеренно ПЕССИМИСТИЧНО (больше, чем нужно), чтобы чанки гарантированно
    помещались в ``num_ctx``.
    """
    text = " ".join((row.text or "").split())
    line = f"{row.index} | {row.created_at:%H:%M:%S} | {text}"
    return max(8, len(line) // 2 + 4)


@dataclass
class SizeReport:
    rows: int
    characters: int
    estimated_tokens: int

    def as_dict(self) -> dict:
        return {
            "rows": self.rows,
            "characters": self.characters,
            "estimated_tokens": self.estimated_tokens,
        }


def measure_rows(rows: Sequence[Row]) -> SizeReport:
    characters = 0
    estimated_tokens = 0
    for r in rows:
        characters += len((r.text or "").strip())
        estimated_tokens += estimate_row_tokens(r)
    return SizeReport(rows=len(rows), characters=characters, estimated_tokens=estimated_tokens)


def log_size(label: str, report: SizeReport, verbose: bool = True) -> None:
    if not verbose:
        return
    print(
        f"{label}: rows={report.rows} "
        f"characters={report.characters} "
        f"estimated_tokens={report.estimated_tokens}"
    )


# ---------------------------------------------------------------------------
# LLM-контракт segmentation
# ---------------------------------------------------------------------------

class LLM(Protocol):
    """Минимальный интерфейс, который segmentation требует от LLM-клиента.

    Реализация — ``OllamaClient.chat_json(system, user, ...) -> {"content": str, ...}``.
    В тестах подменяется fake-объектом с этим же методом (kwargs опциональны).
    """

    def chat_json(self, system: str, user: str, **kwargs) -> dict: ...


def _clean_segment_type(value) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v in SEGMENT_TYPES:
        return v
    # допускаем близкие синонимы
    synonyms = {
        "client": "dialog", "customer": "dialog", "client_dialog": "dialog",
        "staff": "employee", "staff_to_staff": "employee",
        "noise": "background", "irrelevant": "background",
        "unclear": "unknown",
    }
    return synonyms.get(v, None)


def _clean_confidence(value, default: float = 0.0) -> float:
    try:
        c = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, c))


def parse_segmentation_response(content: str, expected_rows: int, fallback_type: str = "unknown") -> list[dict]:
    """Разбирает ответ LLM в список «сырых» сегментов:
    ``[{"start_row":int,"end_row":int,"type":str|None,"confidence":float,"reason":str|None}]``.

    Допускает markdown/noise вокруг JSON. Валидация границ (отрицательные,
    пересечения, пропуски) выполняется дальше в ``repair_segments``.
    """
    from .llm import extract_json

    obj = extract_json(content or "")
    if not obj:
        return []
    raw_segments = obj.get("segments")
    if not isinstance(raw_segments, list):
        return []

    out: list[dict] = []
    for item in raw_segments:
        if not isinstance(item, dict):
            continue
        try:
            start = int(item.get("start_row"))
            end = int(item.get("end_row"))
        except (TypeError, ValueError):
            continue
        if start < 0 or end < start:
            continue
        out.append(
            {
                "start_row": start,
                "end_row": end,
                "type": _clean_segment_type(item.get("type")) or fallback_type,
                "confidence": _clean_confidence(item.get("confidence"), 0.5),
                "reason": item.get("reason") if isinstance(item.get("reason"), str) else None,
            }
        )
    return out


def repair_segments(raw: list[dict], total_rows: int, fallback_type: str = "unknown") -> list[dict]:
    """Приводит «сырые» сегменты к единому покрывающему потоку строк 0..N-1.

    Инвариант валидного результата:
        union == [0, N-1],
        per-segment: 0 <= start <= end < N,
        последовательные сегменты: next.start_row == prev.end_row + 1.

    Правила ремонта (порядок):
        1. Сортировка по start_row (stable), дубликаты по тому же start_row —
           оставляем первый (больший span), остальные отбрасываем.
        2. Обрезка вне диапазона [0, N-1].
        3. Слияние пересекающихся сегментов по перекрыванию в диапазоне строк.
        4. Заполнение «дыр» (пропусков между сегментами) одиночными
           ``background``/``unknown``-сегментами с указанием, что строки были
           не размечены LLM-ом — это гарантирует покрытие ВСЕХ строк
           (требование №4: ни одна строка не пропадает).
        5. Заполнение перед первым сегментом и после последнего.
    """
    if total_rows <= 0:
        return []

    # 1-2: нормализация + удаление пустых / дубликатов с меньшим span
    norm: list[dict] = []
    for s in sorted(raw, key=lambda x: (x["start_row"], -x["end_row"])):
        start = max(0, int(s["start_row"]))
        end = min(total_rows - 1, int(s["end_row"]))
        if end < start:
            continue
        norm.append({
            "start_row": start,
            "end_row": end,
            "type": s.get("type") or fallback_type,
            "confidence": s.get("confidence", 0.4),
            "reason": s.get("reason"),
        })
    # remove strict duplicates (same range) and those fully inside a previous
    dedup: list[dict] = []
    for s in norm:
        if dedup and dedup[-1]["start_row"] <= s["start_row"] <= dedup[-1]["end_row"]:
            # s внутри либо сдвиг — расширяем предыдущий к max(end)
            if s["end_row"] > dedup[-1]["end_row"]:
                dedup[-1]["end_row"] = s["end_row"]
            continue
        dedup.append(s)
    dedup[:] = [s for s in dedup if s["end_row"] >= s["start_row"]]

    # 3: слияние ТОЛЬКО реально пересекающихся сегментов
    # (adjacent = next.start == prev.end + 1 НЕ считается пересечением и сохраняется)
    merged: list[dict] = []
    for s in dedup:
        if merged and s["start_row"] <= merged[-1]["end_row"]:
            if s["end_row"] > merged[-1]["end_row"]:
                merged[-1]["end_row"] = s["end_row"]
            merged[-1]["confidence"] = min(merged[-1]["confidence"], s["confidence"])
            if s.get("type") in ("dialog", "employee") and merged[-1].get("type") in ("unknown", "background"):
                merged[-1]["type"] = s["type"]
            if not merged[-1].get("reason") and s.get("reason"):
                merged[-1]["reason"] = s["reason"]
        else:
            merged.append(dict(s))

    # 4-5: заполнение дыр
    final: list[dict] = []
    cursor = 0
    for s in merged:
        if s["start_row"] > cursor:
            final.append({
                "start_row": cursor,
                "end_row": s["start_row"] - 1,
                "type": fallback_type,
                "confidence": 0.0,
                "reason": "rows not covered by LLM segmentation — placeholder",
            })
        final.append(dict(s))
        cursor = s["end_row"] + 1
    if cursor < total_rows:
        final.append({
            "start_row": cursor,
            "end_row": total_rows - 1,
            "type": fallback_type,
            "confidence": 0.0,
            "reason": "rows not covered by LLM segmentation — placeholder",
        })

    # sanity: invariant
    prev_end = -1
    for s in final:
        assert s["start_row"] == prev_end + 1, (prev_end, s)
        assert 0 <= s["start_row"] and s["end_row"] < total_rows
        prev_end = s["end_row"]
    return final


# ---------------------------------------------------------------------------
# Chunking и hierarchical segmentation
# ---------------------------------------------------------------------------

@dataclass
class SegmentationRequest:
    """Один chunk-запрос к LLM с размером."""

    chunk_rows: list[Row]
    prompt: str
    size: SizeReport
    chunk_index: int
    chunk_count: int
    is_seam: bool  # True если это стыковой (с context) запрос
    overlap_head: int = 0  # сколько строк overlap-контекста в head-склейке
    overlap_tail: int = 1  # сколько строк tail-контекста в tail-склейке


def build_chunks(
    rows: list[Row],
    target_tokens: int,
    seam_context: int = 8,
) -> list[tuple[list[Row], bool]]:
    """Делит поток на чанки по объёму (пессимистично по токенам).

    Возвращает ``[(chunk_rows, is_seam), ...]``. ``is_seam=True`` означает,
    что в этом чанке есть «стык» (или начало, или конец), требующий
    дополнительной проверки после LLM-ответа.

    Чанки делятся на границах реплик; не пытаются резать посреди реплики.
    Если ``target_tokens`` достаточно большой — один чанк.
    """
    if not rows:
        return []

    # первый проход: суммарная оценка
    total_tokens = sum(estimate_row_tokens(r) for r in rows)
    if total_tokens <= target_tokens:
        return [(list(rows), True)]  # один «чистый» чанк, помечен как стыковой только формально

    # реальный расчёт: накопление строк до лимита
    chunks: list[list[Row]] = []
    cur: list[Row] = []
    cur_tokens = 0
    for r in rows:
        r_tokens = estimate_row_tokens(r)
        if cur and cur_tokens + r_tokens > target_tokens:
            chunks.append(cur)
            cur = []
            cur_tokens = 0
        cur.append(r)
        cur_tokens += r_tokens
    if cur:
        chunks.append(cur)

    # маркируем: первый и последний чанки — «стыковые» (требуют проверки);
    # также, если чанк слишком короткий (меньше seam_context строк в начале
    # или конце), это стык.
    def _is_seam(ch: list[Row], idx: int) -> bool:
        if idx == 0 or idx == len(chunks) - 1:
            return True
        return len(ch) < seam_context

    return [(ch, _is_seam(ch, i)) for i, ch in enumerate(chunks)]


def _format_rows_for_prompt(rows: list[Row]) -> str:
    lines = []
    for r in rows:
        text = " ".join((r.text or "").split())
        lines.append(f"{r.index} | {r.created_at:%H:%M:%S} | {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Длительность-порог и Re-split длинных dialog-сегментов
# ---------------------------------------------------------------------------

# Данные (2026-08-25..2026-09-01, NEW pipeline):
#   p50=5.0m  p75=11.1m  p90=18.2m  p95=22.8m  p99=37.8m  max=58.7m
# => 30 min  ≈ p95 * 1.3 : «подозрительно длинный», требует проверки
# => 60 min  ≈ p99 * 1.6: «практически наверняка несколько клиентов»
# Пороги НЕ жёсткие: мы НЕ режем после N минут механически. LLM решает по смыслу
# (завершение разговора, новое приветствие, завершённая покупка + новый клиент),
# и МОЖЕТ вернуть segments:[] для действительно одного длинного разговора.
REVIEW_LONG_SEC = 30 * 60
VERY_LONG_SEC = 60 * 60
# Лимит повторных обращений к LLM на один сегмент (защита от зацикливания).
MAX_RESEGMENT_ATTEMPTS = 3


def _parse_resegment_response(
    content: str,
    segment: "Segment",
    fallback_type: str = "dialog",
) -> list[dict] | None:
    """Разбирает ответ LLM re-segmentation.

    Контракт возврата:
      * ``[]``              — «оставить как есть» (LLM не нашёл внутренних границ);
      * ``[seg, ...]``      — список из 2+ локальных сегментов, покрывающих РОВНО
                              ``[0..L-1]`` без пересечений/пропусков;
      * ``None``            — некорректный ответ (невозможно применить),
                              нужно повторить запрос.
    """
    from .llm import extract_json
    obj = extract_json(content or "")
    if not isinstance(obj, dict) or obj.get("segments") is None:
        return None
    raw = obj["segments"]
    if not isinstance(raw, list):
        return None
    L = segment.end_index - segment.start_index + 1  # локальная длина
    if L <= 0:
        return None
    if len(raw) == 0:
        return []  # явный «оставить как есть»
    if len(raw) == 1:
        # одиночный segment покрывающий весь блок => «оставить как есть»;
        # если не покрывает — невалиден (невозможно применить).
        item = raw[0]
        if not isinstance(item, dict):
            return None
        try:
            s = int(item.get("start_row"))
            e = int(item.get("end_row"))
        except (TypeError, ValueError):
            return None
        # допустим +/-2 строки по краям (LLM может сбить на 1-2 реплики)
        if (segment.start_index - 2 <= s <= segment.start_index + 2 and
                segment.end_index - 2 <= e <= segment.end_index + 2):
            return []
        return None
    # N >= 2 сегмента: нормализуем границы, проверяем покрытие [seg.start..seg.end]
    local_raw: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        try:
            s = int(item.get("start_row"))
            e = int(item.get("end_row"))
        except (TypeError, ValueError):
            return None
        if e < s:
            return None
        # сегменты обязаны лежать ВНУТРИ исходного блока
        if e < segment.start_index or s > segment.end_index:
            return None
        s = max(s, segment.start_index)
        e = min(e, segment.end_index)
        stype = _clean_segment_type(item.get("type")) or fallback_type
        if stype in ("background", "unknown"):
            # внутри клиентского длительного фрагмента background/unknown
            # почти всегда = разговор клиента/сотрудников. Переименовываем,
            # чтобы classification-этап не ушёл в «отказ».
            stype = "dialog"
        local_raw.append({
            "start_row": s - segment.start_index,
            "end_row": e - segment.start_index,
            "type": stype,
            "confidence": _clean_confidence(item.get("confidence"), 0.5),
            "reason": item.get("reason") if isinstance(item.get("reason"), str) else None,
        })
    repaired = repair_segments(local_raw, total_rows=L, fallback_type=fallback_type)
    # обязательный инвариант: coverage [0..L-1] без дублей и пропусков
    covered = sorted(i for r in repaired for i in range(r["start_row"], r["end_row"] + 1))
    if covered != list(range(L)):
        return None
    # repair_segments уже гарантирует порядок; дополнительная проверка:
    prev = -1
    for r in repaired:
        if r["start_row"] != prev + 1:
            return None
        prev = r["end_row"]
    if prev != L - 1:
        return None
    # если все N сегментов случайно сошлись в ОДИН [0..L-1] — это «оставить как есть»
    if len(repaired) == 1 and repaired[0]["start_row"] == 0 and repaired[0]["end_row"] == L - 1:
        return []
    return repaired


def resplit_long_segments(
    segments: list[Segment],
    rows: list[Row],
    llm: "LLM | None",
    store_id: str = "",
    thresholds: tuple[int, int] = (REVIEW_LONG_SEC, VERY_LONG_SEC),
    verbose: bool = True,
) -> tuple[list[Segment], list[dict]]:
    """Прогоняет каждый «подозрительно длинный» dialog-сегмент через LLM-re-split.

    Возвращает ``(new_segments, metrics)``:
      * ``new_segments`` — новый список сегментов (покрытие [0..N-1], без
        дублей/пропусков — инвариант сохраняется всегда).
      * ``metrics``      — статистика: какие сегменты были проверены, сколько
        LLM-вызовов, сколько получилось разбито.

    Если ``llm is None`` — сегменты не меняются (fallback для test/offline).
    """
    if llm is None or not segments:
        return segments, {"candidates": 0, "resegmented": 0, "llm_calls": 0,
                          "split_from": [], "kept_single": []}

    from .prompt import RESEGMENT_LONG_SYSTEM_PROMPT, build_long_dialog_resegment_prompt

    review_min, very_min = thresholds
    new_segments: list[Segment] = []
    candidates = 0
    resegmented = 0
    llm_calls = 0
    split_from: list[dict] = []
    kept_single: list[dict] = []

    # Идём по сегментам в исходном порядке. Каждый «подозрительно длинный»
    # dialog-сегмент пытаемся разбить через LLM:
    #   []      -> оставить как есть (один непрерывный разговор);
    #   [seg..] -> разбить на N>=2 (несколько независимых клиентов);
    #   None    -> LLM не дал корректный ответ -> оставить как есть.
    for seg in segments:
        if seg.segment_type != "dialog":
            new_segments.append(seg)
            continue
        dur = seg.duration_sec
        if dur < review_min:
            # не «подозрительно длинный» — пропускаем
            new_segments.append(seg)
            continue
        candidates += 1
        is_very_long = dur >= very_min
        seg_rows = rows[seg.start_index: seg.end_index + 1]

        # 3-состоятельный результат LLM-проверки:
        #   []      -> «оставить как есть» (один разговор)
        #   [seg..] -> разбить на N>=2
        #   None    -> невалидный ответ, пробуем ещё
        keep: bool | None = None
        split_parts: list[dict] | None = None
        last_err: Exception | None = None
        for _attempt in range(MAX_RESEGMENT_ATTEMPTS):
            user = build_long_dialog_resegment_prompt(store_id, seg_rows, is_very_long=is_very_long)
            # PRODUCTION-LIKE budget (matching segmentation primary call):
            # thinking-модель qwen3.8:27b требует num_ctx/num_predict с запасом,
            # иначе ответ не помещается в контекст и JSON не возвращается.
            # Окружение позволяет переопределить (OFFLINE_NUM_CTX / OFFLINE_NUM_PREDICT).
            import os as _os
            num_ctx = int(_os.getenv("OFFLINE_NUM_CTX", "131072"))
            num_predict = int(_os.getenv("OFFLINE_NUM_PREDICT", "128000"))
            try:
                resp = llm.chat_json(RESEGMENT_LONG_SYSTEM_PROMPT, user,
                                     num_ctx=num_ctx, num_predict=num_predict)
                content = resp.get("content", "") if isinstance(resp, dict) else ""
            except TypeError:
                # старый fake-клиент без kwargs — перепроверяем без именованных параметров
                resp = llm.chat_json(RESEGMENT_LONG_SYSTEM_PROMPT, user)
                content = resp.get("content", "") if isinstance(resp, dict) else ""
            except Exception as exc:  # noqa: BLE001 — логируем, пробуем ещё
                last_err = exc
                continue
            llm_calls += 1
            parsed = _parse_resegment_response(content, seg, fallback_type="dialog")
            if parsed is None:
                last_err = ValueError("invalid response / cannot cover segment [0..L-1]")
                if verbose:
                    print(f"[re-split] {seg.segment_id}: retry ({last_err})")
                continue
            if parsed == []:
                keep = True
            else:
                split_parts = parsed
            break

        if split_parts:
            # Разбили на N>=2. Строим новые Segment-объекты (global indices) и заменяем.
            resegmented += 1
            for i, r in enumerate(split_parts):
                local_start = r["start_row"]
                local_end = r["end_row"]
                new_segments.append(Segment(
                    segment_id=f"{seg.segment_id}r{i + 1}",
                    store_id=store_id,
                    start_index=seg.start_index + local_start,
                    end_index=seg.start_index + local_end,
                    segment_type=r.get("type", "dialog"),
                    confidence=r.get("confidence", 0.4),
                    reason=r.get("reason"),
                    rows=[rows[seg.start_index + local_start + k]
                          for k in range(local_end - local_start + 1)],
                ))
            split_from.append({
                "segment_id": seg.segment_id,
                "start_index": seg.start_index,
                "end_index": seg.end_index,
                "duration_sec": round(dur, 1),
                "subsegments": len(split_parts),
            })
            if verbose:
                print(f"[re-split] {seg.segment_id} ({dur/60:.1f} min) -> {len(split_parts)} сегментов; "
                      f"было: {(seg.reason or '')[:50]}...")
        else:
            # «оставить как есть» либо LLM не смог дать корректный ответ
            new_segments.append(seg)
            kept_single.append({
                "segment_id": seg.segment_id,
                "start_index": seg.start_index,
                "end_index": seg.end_index,
                "duration_sec": round(dur, 1),
                "last_error": str(last_err) if last_err else None,
            })
            if verbose and keep is None:
                print(f"[re-split] {seg.segment_id} ({dur/60:.1f} min): оставлен "
                      f"(LLM: {last_err})")

    metrics = {
        "candidates": candidates,
        "resegmented": resegmented,
        "llm_calls": llm_calls,
        "split_from": split_from,
        "kept_single": kept_single,
        "thresholds": {"review_sec": review_min, "very_long_sec": very_min},
    }

    # Финальный инвариант: покрытие [0..N-1] (как в segment_rows)
    prev = -1
    for s in new_segments:
        assert s.start_index == prev + 1, (prev, s)
        prev = s.end_index
    if rows:
        assert prev == len(rows) - 1, (prev, len(rows))
    return new_segments, metrics


# ---------------------------------------------------------------------------
# Duration-metrics (диагностика качества сегментации)
# ---------------------------------------------------------------------------

def duration_metrics(segments: list[Segment]) -> dict:
    """Диагностические метрики длительности dialog-сегментов.

    Включает min, max, median, p90, p95 и количество >30min/>60min/>90min/>120min.
    Используется в отчёте (debug JSON + print_stats) для контроля.
    """
    import statistics
    durs = [s.duration_sec for s in segments if s.segment_type == "dialog"]
    if not durs:
        return {
            "count": 0, "min_sec": None, "max_sec": None,
            "median_sec": None, "p90_sec": None, "p95_sec": None,
            "gt_30min": 0, "gt_60min": 0, "gt_90min": 0, "gt_120min": 0,
        }
    durs_sorted = sorted(durs)
    n = len(durs_sorted)
    def _q(q):
        # p90, p95 — nearest-rank
        k = max(1, int(round(q / 100.0 * n)))
        return durs_sorted[k - 1]
    return {
        "count": n,
        "min_sec": round(durs_sorted[0], 1),
        "max_sec": round(durs_sorted[-1], 1),
        "median_sec": round(statistics.median(durs), 1),
        "p90_sec": round(_q(90), 1),
        "p95_sec": round(_q(95), 1),
        "gt_30min": sum(1 for x in durs if x > 30 * 60),
        "gt_60min": sum(1 for x in durs if x > 60 * 60),
        "gt_90min": sum(1 for x in durs if x > 90 * 60),
        "gt_120min": sum(1 for x in durs if x > 120 * 60),
    }


# ---------------------------------------------------------------------------
# Основная LLM-сегментация
# ---------------------------------------------------------------------------

def _segmentation_system_prompt(store_id: str | None) -> str:
    from .prompt import SEGMENTATION_SYSTEM_PROMPT
    return SEGMENTATION_SYSTEM_PROMPT


def _segmentation_user_prompt(store_id: str, rows: list[Row], chunk_index: int, chunk_count: int,
                              sales_snippet: str | None = None) -> str:
    from .prompt import build_segmentation_user_prompt
    return build_segmentation_user_prompt(
        store_id=store_id,
        rows=rows,
        chunk_index=chunk_index,
        chunk_count=chunk_count,
        sales_snippet=sales_snippet,
    )


def _segment_one_chunk(
    llm: LLM,
    rows: list[Row],
    store_id: str,
    chunk_index: int,
    chunk_count: int,
    fallback_type: str = "unknown",
    sales_snippet: str | None = None,
) -> list[dict]:
    """Сегментирует один чанк через LLM и возвращает «сырые» сегменты
    (индексы — глобальные, т.к. Row.index уже глобальный)."""

    system = _segmentation_system_prompt(store_id)
    user = _segmentation_user_prompt(store_id, rows, chunk_index, chunk_count, sales_snippet)
    # Сегментация шумного дня генерирует МНОГО сегментов (коротких), поэтому
    # выходной бюджет берём с запасом. num_ctx чуть больше входа, чтобы хватило
    # на prompt + answer. kwargs передаём осторожно: старые fake-клиенты могут
    # не принимать именованные параметры.
    try:
        resp = llm.chat_json(system, user, num_ctx=64000, num_predict=16000)
    except TypeError:
        resp = llm.chat_json(system, user)
    content = resp.get("content", "") if isinstance(resp, dict) else ""
    return parse_segmentation_response(content, expected_rows=len(rows), fallback_type=fallback_type)


def segment_rows(
    rows: list[Row],
    llm: "LLM | None" = None,
    store_id: str = "",
    target_chunk_tokens: int = 32000,
    seam_context: int = 8,
    fallback_type: str = "unknown",
    verbose: bool = True,
    retries: int = 2,
    resplit_long: bool = True,
    resplit_thresholds: tuple[int, int] = (REVIEW_LONG_SEC, VERY_LONG_SEC),
    metrics_sink: dict | None = None,
    sales_snippet: str | None = None,
) -> list[Segment]:
    """Сегментирует поток строк дня.

    ``llm=None`` => deterministic fallback: каждая реплика = свой segment
    (type=fallback_type, confidence=0.0), покрытие гарантировано. Это
    поведение по умолчанию для unit-тестов и для отладки без Ollama.

    ``llm`` реализует Protocol ``LLM`` (метод ``chat_json``).

    ``resplit_long`` (default=True) — выполняет дополнительный LLM-проход
    над «подозрительно длинными» dialog-сегментами (``resplit_thresholds``)
    для обнаружения нескольких последовательных клиентских взаимодействий
    внутри одного блока. Отключается в юнит-тестах и при llm=None.
    """
    if not rows:
        return []

    total = len(rows)
    size = measure_rows(rows)
    log_size("DAY", size, verbose=verbose)

    if llm is None:
        seg_defs = [
            {"start_row": i, "end_row": i, "type": fallback_type,
             "confidence": 0.0, "reason": "no LLM provided — fallback single-row segment"}
            for i in range(total)
        ]
        repaired = repair_segments(seg_defs, total, fallback_type=fallback_type)
    else:
        chunks = build_chunks(rows, target_tokens=target_chunk_tokens, seam_context=seam_context)
        raw_all: list[dict] = []
        for idx, (chunk_rows, _is_seam) in enumerate(chunks):
            chunk_size = measure_rows(chunk_rows)
            log_size(f"SEGMENTATION REQUEST #{idx + 1}/{len(chunks)} (seam={_is_seam})", chunk_size, verbose=verbose)
            chunk_raw: list[dict] | None = None
            last_err: Exception | None = None
            for attempt in range(retries + 1):
                try:
                    candidate = _segment_one_chunk(
                        llm, chunk_rows, store_id, idx + 1, len(chunks), fallback_type,
                        sales_snippet=sales_snippet,
                    )
                    if candidate:
                        chunk_raw = candidate
                        break
                    last_err = ValueError("empty segmentation from LLM")
                except Exception as exc:  # noqa: BLE001 — логируем и пробуем ещё
                    last_err = exc
                if verbose:
                    print(f"[seg] request #{idx + 1} attempt {attempt + 1}: {last_err}")
            if chunk_raw is None:
                # Fallback: каждую строку чанка -> single-row segment (покрытие гарантируется,
                # мы не делаем один огромный "unknown" блок на весь чанк).
                chunk_raw = [
                    {"start_row": r.index, "end_row": r.index,
                     "type": fallback_type, "confidence": 0.0,
                     "reason": f"LLM error: {last_err}"}
                    for r in chunk_rows
                ]
            raw_all.extend(chunk_raw)

        # глобальный ремонт: слияние через стыки, заделка дыр
        repaired = repair_segments(raw_all, total, fallback_type=fallback_type)

    # построение Segment-объектов (ссылаемся на исходные Row-объекты)
    segments: list[Segment] = []
    for i, s in enumerate(repaired):
        segment_rows = [rows[idx] for idx in range(s["start_row"], s["end_row"] + 1)]
        segments.append(
            Segment(
                segment_id=f"segment_{i + 1:03d}",
                store_id=store_id,
                start_index=s["start_row"],
                end_index=s["end_row"],
                segment_type=s.get("type") or fallback_type,
                confidence=float(s.get("confidence", 0.0)),
                reason=s.get("reason"),
                rows=segment_rows,
            )
        )

    # инвариант покрытия: union == [0, total-1], без пересечений
    prev = -1
    for seg in segments:
        assert seg.start_index == prev + 1, (prev, seg)
        prev = seg.end_index
    assert prev == total - 1

    # Дополнительный прогон над «подозрительно длинными» dialog-сегментами:
    # ищем несколько последовательных клиентских взаимодействий внутри
    # одного блока (завершение + новое появление/новая покупка).
    # Если LLM вернёт корректный набор N>=2 тилей — заменяем сегмент,
    # иначе оставляем как есть. В любом случае инвариант покрытия сохраняется
    # (re-split использует только валидные tile-совокупности).
    if resplit_long and llm is not None and any(
        s.segment_type == "dialog" and s.duration_sec >= resplit_thresholds[0]
        for s in segments
    ):
        segments, resplit_metrics = resplit_long_segments(
            segments, rows, llm,
            store_id=store_id,
            thresholds=resplit_thresholds,
            verbose=verbose,
        )
        if metrics_sink is not None:
            # Акумулируем метрики (по несколько магазинов):
            #   int/float -> сумма; list -> extend; dict -> update/merge.
            for k, v in resplit_metrics.items():
                if isinstance(v, (int, float)):
                    metrics_sink[k] = metrics_sink.get(k, 0) + v
                elif isinstance(v, list):
                    existing = metrics_sink.get(k) or []
                    existing.extend(v)
                    metrics_sink[k] = existing
                elif isinstance(v, dict):
                    existing = metrics_sink.get(k) or {}
                    existing.update(v)
                    metrics_sink[k] = existing
    return segments


# ---------------------------------------------------------------------------
# Ссылки на строки (обёртка, чтобы Segment не хранил Row-объекты)
# ---------------------------------------------------------------------------

def rows_for_segment(rows: list[Row], seg: Segment) -> list[Row]:
    """Возвращает список Row для сегмента (по row_indices)."""
    return [rows[i] for i in seg.row_indices]


# ---------------------------------------------------------------------------
# Обратная совместимость со старым API (test-совместимость и упрощение)
# ---------------------------------------------------------------------------

@dataclass
class Block:
    """Устаревший «блок»: сохранён только для обратной совместимости
    с legacy-тестами и кодом, который ещё использует старый интерфейс.
    В новом pipeline НЕ используется.
    """

    store_id: str
    seller_id: str | None
    rows: list[Row]
    client_id_hint: str | None = None  # deprecated: НЕ используется в segmentation

    @property
    def start(self) -> datetime:
        return self.rows[0].created_at

    @property
    def end(self) -> datetime:
        return self.rows[-1].created_at

    @property
    def duration_sec(self) -> float:
        return float((self.end - self.start).total_seconds())

    @property
    def text(self) -> str:
        parts = [r.text.strip() for r in self.rows if r.text and r.text.strip()]
        return "\n".join(parts)

    @property
    def session_ids(self) -> list[str]:
        result: list[str] = []
        seen = set()
        for r in self.rows:
            if r.session_id and r.session_id not in seen:
                seen.add(r.session_id)
                result.append(r.session_id)
        return result

    @property
    def is_sale_any(self) -> bool:
        return any(r.is_sale for r in self.rows)

    @property
    def alarm_any(self) -> bool:
        return any(r.is_alarm_triggered for r in self.rows)

    @property
    def dialog_types(self) -> list[str]:
        result: list[str] = []
        for r in self.rows:
            if r.dialog_type and r.dialog_type not in result:
                result.append(r.dialog_type)
        return result


def build_blocks(rows: list[Row], store_id: str) -> list[Block]:
    """LEGACY: строит блоки по последовательности реплик (по одной строке-диапазону).

    В новом pipeline (offline segmentation на LLM) эта функция не вызывается.
    Поддержана только для обратной совместимости с тестами, которые
    исторически опирались на старый интерфейс; поведение — каждая строка
    становится отдельным блоком, client_id НЕ используется.
    """
    blocks: list[Block] = []
    for i, r in enumerate(rows):
        blocks.append(Block(store_id=store_id, seller_id=r.seller_id, rows=[r], client_id_hint=None))
    return blocks
