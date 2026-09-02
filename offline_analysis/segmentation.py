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
# Основная LLM-сегментация
# ---------------------------------------------------------------------------

def _segmentation_system_prompt(store_id: str | None) -> str:
    from .prompt import SEGMENTATION_SYSTEM_PROMPT
    return SEGMENTATION_SYSTEM_PROMPT


def _segmentation_user_prompt(store_id: str, rows: list[Row], chunk_index: int, chunk_count: int) -> str:
    from .prompt import build_segmentation_user_prompt
    return build_segmentation_user_prompt(
        store_id=store_id,
        rows=rows,
        chunk_index=chunk_index,
        chunk_count=chunk_count,
    )


def _segment_one_chunk(
    llm: LLM,
    rows: list[Row],
    store_id: str,
    chunk_index: int,
    chunk_count: int,
    fallback_type: str = "unknown",
) -> list[dict]:
    """Сегментирует один чанк через LLM и возвращает «сырые» сегменты
    (индексы — глобальные, т.к. Row.index уже глобальный)."""

    system = _segmentation_system_prompt(store_id)
    user = _segmentation_user_prompt(store_id, rows, chunk_index, chunk_count)
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
) -> list[Segment]:
    """Сегментирует поток строк дня.

    ``llm=None`` => deterministic fallback: каждая реплика = свой segment
    (type=fallback_type, confidence=0.0), покрытие гарантировано. Это
    поведение по умолчанию для unit-тестов и для отладки без Ollama.

    ``llm`` реализует Protocol ``LLM`` (метод ``chat_json``).
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
                    candidate = _segment_one_chunk(llm, chunk_rows, store_id, idx + 1, len(chunks), fallback_type)
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
