"""Человекочитаемый PDF-отчёт по диалогам (на основе тех же данных, что и Excel).

Модуль **НЕ** переиспользует LLM-запросы: он оперирует уже сегментированными и
уже классифицированными данными (списком `final_rows` + необязательным lookup'ом
исходных реплик по диалогу).

Публичный API:

    generate_pdf_report(rows, output_path, report_date=None, *, rows_lookup=None, title=None)
        -> Path (путь к созданному PDF)

Вся остальная логика (статистика, извлечение реплик, санитизирование вывода
модели) — чистые функции, чтобы их легко тестировать без reportlab.

Что НЕ выводится в PDF:
    * `session_ids` и любые технические идентификаторы сессий;
    * `client_id` как идентификатор диалога (в PDF используется порядковый номер
      «Диалог N», не client_id, не old client_id, не store_id как "ключ");
    * chain-of-thought / скрытый reasoning модели (если `model_reasoning`
      распознаётся как CoT или слишком длинный — заменяется на короткий
      безопасный `classification_reason`).

Используется та же библиотека PDF, что и в остальном проекте — ReportLab
(см. stats_service/requirements.txt). Шрифт кириллицы — DejaVu Sans (в Debian
доступен через `fonts-dejavu`, уже установлен в Dockerfile `stats_service`).
"""

from __future__ import annotations

import html
from datetime import date, datetime
from pathlib import Path

# reportlab — опционально на импорте: модуль может быть вставлен в тест-окружение
# без установленной reportlab, чтобы чистый API (статистика, извлечение реплик)
# оставался тестируемым. PDF генерация требует reportlab — проверяется в
# `generate_pdf_report`.
try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        HRFlowable,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )
    _HAS_REPORTLAB = True
except ImportError:  # pragma: no cover - зависит от окружения
    _HAS_REPORTLAB = False


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

_DEFAULT_TITLE = "Отчёт по диалогам"
_NO_VALUE = "—"
_UTTANCE_LABEL = "Реплика"  # нейтральная метка, когда роль строки не известна

# Ожидание: classification не заполняет per-line роли — поэтому для каждой
# реплики используем единую нейтральную маркировку.
_DIALOG_TYPE_LABELS = {
    "buy": "продажи",
    "service": "сервисные обращения",
    "help": "помощь",
    "corporate": "корпоративные",
}
_OTHER_LABEL_KEY = "прочие"

# Подмножество ключевых слов, которые указывают на CoT / внутренний reasoning
# модели, который нельзя показывать пользователю в PDF.
_COT_HINTS = (
    "шаг 1", "шаг 2", "шаг 3", "шаг 4", "шаг 5",
    "step 1", "step 2", "step 3", "step 4", "step 5",
    "let me think", "let's think", "let me try", "let's try",
    "thinking about", "first step", "second step",
    "пошагово", "по-шагам", "по-ш-а-г-а-м",
    "сначала я", "потом я", "и тогда я", "наконец я",
    "я рассуждаю", "я рассуждал",
    "разберу по шагам", "обдумаю",
    "thinking",
    "reasoning about",
)
_COT_FALLBACK_REASON = (
    "Модель классифицировала этот диалог по типу, роли и признакам "
    "покупки/сервиса, которые были определены в ходе классификации."
)
# Максимальная длина безопасного model_reasoning, который можно показать.
# Свыше этого — скорее всего, CoT или очень развёрнутый "почему".
_MAX_SAFE_REASON_LEN = 600


# ---------------------------------------------------------------------------
# Чистые helpers (тестируются без reportlab)
# ---------------------------------------------------------------------------

def format_report_date(report_date) -> str:
    """Форматирует дату отчёта в DD.MM.YYYY.

    Принимает `date`, `datetime` или строку `YYYY-MM-DD`.
    Строку без даты — бросает ValueError (чтобы не показать "00.00.0000").
    """
    if report_date is None:
        raise ValueError("report_date not provided (cannot format report date)")
    if isinstance(report_date, datetime):
        d = report_date.date()
    elif isinstance(report_date, date):
        d = report_date
    elif isinstance(report_date, str):
        s = report_date.strip()
        if not s:
            raise ValueError("empty report_date string")
        d = datetime.strptime(s, "%Y-%m-%d").date()
    else:
        raise ValueError(f"unsupported report_date type: {type(report_date)!r}")
    return d.strftime("%d.%m.%Y")


def dialog_type_label(dialog_type) -> str:
    """Карта dialog_type (code) -> человекочитаемый Russian label.

    Неизвестные/пустые коды попадают в "прочие", чтобы не терять строку.
    """
    if dialog_type is None:
        return _OTHER_LABEL_KEY
    key = str(dialog_type).strip().lower()
    return _DIALOG_TYPE_LABELS.get(key, _OTHER_LABEL_KEY)


def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y", "t")
    return bool(value)


def _fmt_bool(value) -> str:
    return "Да" if _truthy(value) else "Нет"


def _fmt_time(dt) -> str:
    """HH:MM:SS или «—» если поле пустое/битое."""
    if dt is None:
        return _NO_VALUE
    try:
        if isinstance(dt, datetime):
            return dt.strftime("%H:%M:%S")
        return f"{dt}"
    except Exception:
        return _NO_VALUE


def _fmt_duration(seconds) -> str:
    """Превращает длительность (float секунд) в «M мин S сек»; безопасный fallback."""
    if seconds is None:
        return _NO_VALUE
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return _NO_VALUE
    if total < 0:
        return _NO_VALUE
    minutes, rem = divmod(total, 60)
    if minutes == 0:
        return f"{rem} сек"
    return f"{minutes} мин {rem} сек"


def _looks_like_cot(reason: str) -> bool:
    """Эвристика: содержит ли `reason` признаки chain-of-thought.

    Консервативно: лучше заменить короткий безопасный вывод, чем показать
    CoT. `reason=None` (нет вывода) — это не CoT.
    """
    if not reason:
        return False
    text = str(reason).lower()
    hits = 0
    for hint in _COT_HINTS:
        if hint in text:
            hits += 1
    if hits >= 2:
        return True
    # один кодовый маркер + очень длинный текст -> скорее всего CoT
    if hits >= 1 and len(text) > _MAX_SAFE_REASON_LEN:
        return True
    # очень длинный текст — почти наверняка не короткий business reason
    if len(text) > _MAX_SAFE_REASON_LEN:
        return True
    return False


def safe_classification_reason(row: dict | None, default: str = _COT_FALLBACK_REASON) -> str:
    """Безопасный короткий вывод для PDF.

    `row["model_reasoning"]` используется только если он существует, короткий
    и не похож на chain-of-thought. Иначе — нейтральное стандартное объяснение.
    """
    if row is None:
        return default
    raw = row.get("model_reasoning") if isinstance(row, dict) else getattr(row, "model_reasoning", None)
    if raw is None:
        return default
    text = str(raw).strip()
    if not text:
        return default
    if _looks_like_cot(text):
        return default
    return text


def _extract_utterances(row, rows_lookup: dict | None = None, row_index: int = 0) -> list[dict]:
    """Список реплик для одного диалога -> [{"time": str, "text": str}, ...].

    Приоритет:
      1) `rows_lookup[row_index]` — исходные строки сегмента (с временем);
      2) иначе `row["recognition_text"]`, разбитый по строкам (без времени);
      3) иначе пустой список (вызовущий подставит нейтральную заглушку).
    """
    def _get(obj, name, default=None):
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    if rows_lookup:
        segs = rows_lookup.get(row_index) or []
        out = []
        for r in segs:
            text = _get(r, "text") or ""
            time_val = _get(r, "created_at")
            t = str(text).strip()
            if t:
                out.append({"time": _fmt_time(time_val), "text": t})
        if out:
            return out
    # fallback: строим из recognition_text (каждая строка = одна реплика)
    raw = _get(row, "recognition_text") or ""
    out = []
    for line in str(raw).splitlines():
        line = line.strip()
        if line:
            out.append({"time": _NO_VALUE, "text": line})
    return out


def build_statistics(rows: list[dict]) -> dict:
    """Статистика для первой страницы: количество диалогов по типам + продажи.

    Возвращает dict с ключами:
        label -> count
        "total", "with_sale"
        "label_order" — порядок отчёта (продажи, сервисные, помощь, корпоративные, прочие)
        "percent_<label>" — доля (float 0..1) для каждого label
    """
    if rows is None:
        rows = []
    stats: dict[str, int] = {_OTHER_LABEL_KEY: 0}
    for label in _DIALOG_TYPE_LABELS.values():
        stats[label] = 0
    for row in rows:
        dt = row.get("dialog_type") if isinstance(row, dict) else getattr(row, "dialog_type", None)
        key = dialog_type_label(dt)
        stats[key] = stats.get(key, 0) + 1
    total = len(rows)
    with_sale = sum(1 for r in rows if _truthy(r.get("is_sale") if isinstance(r, dict) else getattr(r, "is_sale", False)))
    # порядок для вывода
    order = [
        "продажи",
        "сервисные обращения",
        "помощь",
        "корпоративные",
        _OTHER_LABEL_KEY,
    ]
    percent = {}
    for k in order:
        percent[k] = (stats.get(k, 0) / total) if total else 0.0
    return {
        "total": total,
        "with_sale": with_sale,
        "counts": dict(stats),
        "percent": percent,
        "label_order": order,
    }


def _escape_xml(value) -> str:
    """Экранирует текст как XML для ReportLab Paragraph (без HTML-интерпретации)."""
    if value is None:
        return ""
    return html.escape(str(value))


def _seller_label(row: dict) -> str:
    seller = row.get("seller_id") if isinstance(row, dict) else getattr(row, "seller_id", None)
    if _is_blank(seller):
        return _NO_VALUE
    return str(seller).strip()


def _store_label(row: dict) -> str:
    store = row.get("store_id") if isinstance(row, dict) else getattr(row, "store_id", None)
    if _is_blank(store):
        return _NO_VALUE
    return str(store).strip()


def _dialog_title(row_index: int) -> str:
    """Заголовок диалога: "Диалог N" (номер 1-based).

    Не используем `client_id`: это технический/внутренний идентификатор.
    """
    return f"Диалог {int(row_index) + 1}"


# ---------------------------------------------------------------------------
# ReportLab-реализация (используются только внутри generate_pdf_report)
# ---------------------------------------------------------------------------

def _register_fonts() -> str:
    """Регистрирует кириллический шрифт (DejaVu Sans). Возвращает имя font_name.

    DejaVu обязателен для корректной кириллицы; если файл не найден — бросаем
    исключение, чтобы не генерировать «битый» PDF (кириллица исчезнет).
    """
    if not _HAS_REPORTLAB:
        raise RuntimeError("reportlab is not installed — cannot generate PDF")
    font_name = "DejaVuSans"
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    )
    for p in candidates:
        if Path(p).exists():
            pdfmetrics.registerFont(TTFont(font_name, p))
            return font_name
    raise RuntimeError(
        "DejaVu Sans font not found under standard paths; install `fonts-dejavu` "
        "to render Cyrillic in the PDF report"
    )


def _build_styles(font_name: str) -> dict:
    """БазовыеParagraphStyle для PDF."""
    title = ParagraphStyle(
        "Title",
        fontName=font_name,
        fontSize=20,
        leading=24,
        spaceAfter=4,
        alignment=TA_LEFT,
        textColor=colors.HexColor("#1F2937"),
    )
    date_style = ParagraphStyle(
        "DateStyle",
        fontName=font_name,
        fontSize=11,
        leading=14,
        textColor=colors.HexColor("#4B5563"),
        spaceAfter=8,
    )
    section = ParagraphStyle(
        "Section",
        fontName=font_name,
        fontSize=13,
        leading=16,
        textColor=colors.HexColor("#111827"),
        spaceBefore=6,
        spaceAfter=3,
    )
    label = ParagraphStyle(
        "MetaLabel",
        fontName=font_name,
        fontSize=9,
        leading=11,
        textColor=colors.HexColor("#4B5563"),
    )
    value = ParagraphStyle(
        "MetaValue",
        fontName=font_name,
        fontSize=9,
        leading=11,
        alignment=TA_LEFT,
    )
    utterance_label = ParagraphStyle(
        "UtteranceLabel",
        fontName=font_name,
        fontSize=9,
        leading=11,
        textColor=colors.HexColor("#374151"),
        spaceBefore=2,
        leftIndent=8,
    )
    utterance_text = ParagraphStyle(
        "UtteranceText",
        fontName=font_name,
        fontSize=10,
        leading=13,
        alignment=TA_LEFT,
        leftIndent=8,
        spaceAfter=3,
    )
    reason_style = ParagraphStyle(
        "Reason",
        fontName=font_name,
        fontSize=10,
        leading=13,
        textColor=colors.HexColor("#1F2937"),
        leftIndent=12,
        spaceBefore=4,
        spaceAfter=8,
    )
    stats_value = ParagraphStyle(
        "StatsValue",
        fontName=font_name,
        fontSize=9,
        leading=11,
        textColor=colors.HexColor("#374151"),
    )
    stats_label = ParagraphStyle(
        "StatsLabel",
        fontName=font_name,
        fontSize=9,
        leading=11,
        textColor=colors.HexColor("#4B5563"),
    )
    stats_total = ParagraphStyle(
        "StatsTotal",
        fontName=font_name,
        fontSize=12,
        leading=15,
        textColor=colors.HexColor("#111827"),
    )
    return {
        "title": title,
        "date": date_style,
        "section": section,
        "label": label,
        "value": value,
        "utterance_label": utterance_label,
        "utterance_text": utterance_text,
        "reason": reason_style,
        "stats_value": stats_value,
        "stats_label": stats_label,
        "stats_total": stats_total,
    }


def _build_title_block(story, styles, title, date_str):
    story.append(Paragraph(_escape_xml(title), styles["title"]))
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(f"Дата: {_escape_xml(date_str)}", styles["date"]))
    story.append(Spacer(1, 4 * mm))


def _build_stats_block(story, styles, stats: dict):
    story.append(Paragraph("Краткая статистика", styles["section"]))
    # строка «всего» — выделенная (топ/бот), метка слева, число справа
    rows = [
        [
            Paragraph(_escape_xml("Всего диалогов"), styles["stats_label"]),
            Paragraph(_escape_xml(str(stats["total"])), styles["stats_total"]),
        ],
    ]
    # строки для каждого типа
    label_to_ru = {
        "продажи": "Продажи",
        "сервисные обращения": "Сервисные обращения",
        "помощь": "Помощь",
        "корпоративные": "Корпоративные",
        "прочие": "Прочие",
    }
    for key in stats["label_order"]:
        ru = label_to_ru.get(key, key)
        count = stats["counts"].get(key, 0)
        pct = stats["percent"].get(key, 0.0)
        pct_str = f"{pct * 100:.1f}%" if stats["total"] else "—"
        rows.append([
            Paragraph(_escape_xml(ru), styles["stats_label"]),
            Paragraph(_escape_xml(f"{count}  ({pct_str})"), styles["stats_value"]),
        ])
    rows.append([
        Paragraph(_escape_xml("Количество диалогов с продажей"), styles["stats_label"]),
        Paragraph(_escape_xml(str(stats["with_sale"])), styles["stats_total"]),
    ])

    table = Table(rows, colWidths=[70 * mm, 70 * mm])
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F3F4F6")),
        ("FONTNAME", (0, 0), (-1, -1), "DejaVuSans"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 1), (-1, -2), 2),
        ("BOTTOMPADDING", (0, 1), (-1, -2), 2),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#FAFAFA")]),
        ("LINEBELOW", (0, 0), (-1, 0), 0.4, colors.HexColor("#D1D5DB")),
        ("LINEBELOW", (0, -2), (-1, -2), 0.4, colors.HexColor("#D1D5DB")),
    ]
    table.setStyle(TableStyle(style))
    story.append(table)
    story.append(Spacer(1, 8 * mm))


def _build_dialog_block(story, styles, row, utterances, title, reason):
    def _get(name, default=None):
        if isinstance(row, dict):
            return row.get(name, default)
        return getattr(row, name, default)

    # Заголовок диалога
    story.append(Paragraph(_escape_xml(title), styles["section"]))
    story.append(Spacer(1, 2 * mm))

    # Метаданные (2 колонки: метка / значение), без технических ID
    dt = _get("dialog_type")
    meta = [
        ("Магазин", _store_label(row) or _NO_VALUE),
        ("Продавец", _seller_label(row) or _NO_VALUE),
        ("Тип", str(dt).strip() if dt not in (None, "") else _NO_VALUE),
        ("Продажа", _fmt_bool(_get("is_sale", False))),
        ("Время", f"{_fmt_time(_get('dialog_start_at'))} — {_fmt_time(_get('dialog_end_at'))}"),
        ("Длительность", _fmt_duration(_get("dialog_duration_sec"))),
    ]
    rows = [
        [
            Paragraph(_escape_xml(label), styles["label"]),
            Paragraph(_escape_xml(str(val) if val not in (None, "") else _NO_VALUE), styles["value"]),
        ]
        for label, val in meta
    ]
    t = Table(rows, colWidths=[32 * mm, None])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(t)
    story.append(Spacer(1, 4 * mm))

    # Реплики
    story.append(Paragraph("Диалог", styles["section"]))
    story.append(Spacer(1, 1 * mm))
    # Каждая реплика — с новой строки. Метка «Реплика:» НЕ выводится:
    # роль говорящего в данных отсутствует, поэтому не выводим ни метку,
    # ни ролик/клиент/продавец. Слова не меняем.
    for utt in utterances:
        story.append(Paragraph(_escape_xml(utt["text"]), styles["utterance_text"]))

    # Вывод модели
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph("Вывод модели", styles["section"]))
    story.append(Paragraph(_escape_xml(reason), styles["reason"]))
    story.append(Spacer(1, 10 * mm))
    # визуальный разделитель между диалогами
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#E5E7EB")))
    story.append(Spacer(1, 10 * mm))


def _on_page(canvas, doc):
    """Номер страницы в футере."""
    page = canvas.getPageNumber()
    canvas.saveState()
    canvas.setFont("DejaVuSans", 8)
    canvas.setFillColor(colors.HexColor("#6B7280"))
    canvas.drawRightString(A4[0] - doc.rightMargin, doc.bottomMargin - 8, str(page))
    canvas.drawString(doc.leftMargin, doc.bottomMargin - 8, "Отчёт по диалогам")
    canvas.restoreState()


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

def generate_pdf_report(
    rows: list[dict],
    output_path: str | Path,
    report_date: date | str | None = None,
    *,
    rows_lookup: dict | None = None,
    title: str = _DEFAULT_TITLE,
) -> Path:
    """Создаёт человекочитаемый PDF-отчёт по диалогам.

    Параметры:
        rows: список row'ов из `build_final_rows` (dict'ы или объекты с полями
              store_id, seller_id, dialog_type, is_sale,
              dialog_start_at, dialog_end_at, dialog_duration_sec,
              recognition_text, model_reasoning).
        output_path: путь, куда писать PDF.
        report_date: date/datetime/str(YYYY-MM-DD). Обязателен.
        rows_lookup: опциональный {index_in_final_rows: [Row, ...]} —
              используется, чтобы отобразить реплики с временным указанием.
              Если absent/falsy, реплики извлекаются из `recognition_text`.
        title: заголовок отчёта (по умолчанию «Отчёт по диалогам»).

    Возвращает: `Path` к созданному PDF.

    Бросает:
        ValueError — если report_date не задан/не распознан.
        RuntimeError — если reportlab не установлен.
    """
    if rows is None:
        rows = []
    out = Path(output_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)

    date_str = format_report_date(report_date)
    stats = build_statistics(rows)
    font_name = _register_fonts() if _HAS_REPORTLAB else "Helvetica"
    styles = _build_styles(font_name)

    doc = SimpleDocTemplate(
        str(out),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=title,
    )
    story = []
    _build_title_block(story, styles, title, date_str)
    _build_stats_block(story, styles, stats)

    for idx, row in enumerate(rows):
        utterances = _extract_utterances(row, rows_lookup, idx)
        if not utterances:
            utterances = [{"time": _NO_VALUE, "text": _NO_VALUE}]
        dialog_title = _dialog_title(idx)
        reason = safe_classification_reason(row)
        _build_dialog_block(story, styles, row, utterances, dialog_title, reason)

    doc.build(
        story,
        onFirstPage=_on_page,
        onLaterPages=_on_page,
    )
    return out
