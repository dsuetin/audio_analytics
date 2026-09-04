"""Тесты generation PDF-отчёта (offline_analysis.pdf_report).

Проверяют требования к PDF:
  1. PDF создаётся;
  2. PDF не пустой;
  3. PDF содержит заголовок и дату;
  4. Количество диалогов в PDF соответствует входным данным;
  5. В PDF нет session_ids (технических идентификаторов сессий);
  6. Длинный диалог корректно переносится на несколько страниц;
  7. Кириллица отображается корректно;
  8. Пустые optional-поля не ломают генерацию.

(9. Отсутствие поломки существующих Excel-тестов — проверяется прогоном
   остальных тестов в репозитории, они НЕ изменены.)

Тесты используют те же уже обработанные данные (dict-rows), как Excel-отчёт,
и НЕ запускают LLM.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from offline_analysis import pdf_report as P
from offline_analysis.pdf_report import (
    build_statistics,
    format_report_date,
    generate_pdf_report,
    safe_classification_reason,
)

# Тест работает даже если в окружении нет reportlab: чистые функции всегда
# тестируем, PDF-тесты пропускаются, только когда нет reportlab/шрифта.
_HAS_REPORTLAB = P._HAS_REPORTLAB


# ---------------------------------------------------------------------------
# Фиксированные данные (имитация final_rows из build_final_rows)
# ---------------------------------------------------------------------------

def _row(
    dialog_type="buy",
    is_sale=False,
    model_reasoning=None,
    store_id="г_Пятигорск_ул_Первомайская_д_3",
    seller_id="иванов_и",
    start=None,
    end=None,
    duration=None,
    rec="Здравствуйте\nсколько стоит АКБ\nда, есть\nберу",
    session_ids=None,
):
    start = start or datetime(2026, 9, 1, 8, 1, 19)
    end = end or datetime(2026, 9, 1, 8, 2, 52)
    return {
        "store_id": store_id,
        "seller_id": seller_id,
        "client_id": None,
        "recognition_text": rec,
        "dialog_type": dialog_type,
        "is_sale": is_sale,
        "loss_reason": None,
        "is_alarm_triggered": False,
        "dialog_start_at": start,
        "dialog_end_at": end,
        "dialog_duration_sec": 93.0 if duration is None else duration,
        "session_ids": session_ids or "20260901-abcdef-12345, 20260901-9abcde-67890",
        "model_reasoning": model_reasoning,
    }


def _rows(n: int = 3):
    return [_row() for _ in range(n)]


def _extract_text(pdf_path: str) -> str:
    """Собирает текст PDF (pypdf)."""
    from pypdf import PdfReader

    reader = PdfReader(pdf_path)
    pages = [(p.extract_text() or "") for p in reader.pages]
    return "\n".join(pages)


def _pdf_page_count(pdf_path: str) -> int:
    from pypdf import PdfReader

    return len(PdfReader(pdf_path).pages)


# ---------------------------------------------------------------------------
# 1-2. Создаётся и не пустой
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_REPORTLAB, reason="reportlab не установлен")
def test_pdf_is_created(tmp_path):
    out = tmp_path / "report.pdf"
    res = generate_pdf_report(_rows(3), str(out), "2026-09-01")
    res = str(res)
    assert out.exists()
    assert out.stat().st_size > 0
    # валидная PDF-заглушка
    with open(res, "rb") as f:
        assert f.read(5) == b"%PDF-"


@pytest.mark.skipif(not _HAS_REPORTLAB, reason="reportlab не установлен")
def test_pdf_is_not_empty(tmp_path):
    out = tmp_path / "report.pdf"
    generate_pdf_report(_rows(3), str(out), "2026-09-01")
    assert out.stat().st_size > 1000  # реальный PDF с контентом заметно больше заглушки


# ---------------------------------------------------------------------------
# 3. Заголовок и дата
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_REPORTLAB, reason="reportlab не установлен")
def test_pdf_contains_title_and_date(tmp_path):
    out = tmp_path / "report.pdf"
    generate_pdf_report(_rows(2), str(out), "2026-09-01")
    text = _extract_text(str(out))
    assert "Отчёт по диалогам" in text
    # формат DD.MM.YYYY по спецификации
    assert "01.09.2026" in text


# ---------------------------------------------------------------------------
# 4. Количество диалогов соответствует входным данным
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_REPORTLAB, reason="reportlab не установлен")
def test_pdf_dialog_count_matches_input(tmp_path):
    rows = _rows(4)
    out = tmp_path / "report.pdf"
    generate_pdf_report(rows, str(out), "2026-09-01")
    text = _extract_text(str(out))
    # «Вывод модели» присутствует ровно в каждом диалоге — надёжная метка
    assert text.count("Вывод модели") == 4
    # заголовок каждого диалога «Диалог N»
    assert "Диалог 1" in text and "Диалог 4" in text
    # статистика: «Всего диалогов 4»
    assert "Всего диалогов" in text and "4" in text


# ---------------------------------------------------------------------------
# 5. Нет session_ids / технических ID
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_REPORTLAB, reason="reportlab не установлен")
def test_pdf_does_not_contain_session_ids(tmp_path):
    session_ids = "20260901-abcdef-12345, 20260901-9abcde-67890"
    rows = [_row(session_ids=session_ids)]
    out = tmp_path / "report.pdf"
    generate_pdf_report(rows, str(out), "2026-09-01")
    text = _extract_text(str(out))
    for fragment in ("20260901-abcdef-12345", "9abcde-67890", "uuid", "session_id"):
        assert fragment not in text, f"неожиданно встретил {fragment!r} в PDF"


# ---------------------------------------------------------------------------
# 6. Длинный диалог переносится на несколько страниц
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_REPORTLAB, reason="reportlab не установлен")
def test_long_dialog_spans_multiple_pages(tmp_path):
    # длинный диалог: ~120 реплик — гарантированно более одной страницы
    long_rec = "\n".join(f"фрагмент речи {i} про аккумулятор" for i in range(120))
    rows = [_row(rec=long_rec)]
    out = tmp_path / "report.pdf"
    generate_pdf_report(rows, str(out), "2026-09-01")
    pages = _pdf_page_count(str(out))
    assert pages >= 2, f"длинный диалог должен занимать несколько страниц, а страниц: {pages}"
    # весь текст присутствует (не обрезан в начале и в конце)
    text = _extract_text(str(out))
    assert "фрагмент речи 0" in text
    assert "фрагмент речи 119" in text


# ---------------------------------------------------------------------------
# 7. Кириллица отображается корректно
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_REPORTLAB, reason="reportlab не установлен")
def test_cyrillic_renders_correctly(tmp_path):
    rows = [_row(rec="Здравствуйте, нужен аккумулятор для машины")]
    out = tmp_path / "report.pdf"
    generate_pdf_report(rows, str(out), "2026-09-01")
    text = _extract_text(str(out))
    # кириллица (а) должна присутствовать в извлечённом тексте, (б) без «кракозябр»
    assert "аккумулятор" in text
    assert "Здравствуйте" in text
    # отсутствие типичных артефактов неправильной кодировки
    assert "Ã" not in text and "Â" not in text


# ---------------------------------------------------------------------------
# 8. Пустые optional-поля не ломают генерацию
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_REPORTLAB, reason="reportlab не установлен")
def test_empty_optional_fields_do_not_break(tmp_path):
    rows = [{
        "store_id": None,
        "seller_id": None,
        "client_id": None,
        "recognition_text": None,
        "dialog_type": None,
        "is_sale": False,
        "loss_reason": None,
        "is_alarm_triggered": False,
        "dialog_start_at": None,
        "dialog_end_at": None,
        "dialog_duration_sec": None,
        "session_ids": None,
        "model_reasoning": None,
    }]
    out = tmp_path / "empty.pdf"
    res = generate_pdf_report(rows, str(out), "2026-09-01")
    assert out.exists()
    assert out.stat().st_size > 0
    text = _extract_text(str(out))
    # Пустой текст реплик заменяется заглушкой «—»; метка «Реплика:» в PDF не выводится
    assert "—" in text
    assert "Реплика" not in text


@pytest.mark.skipif(not _HAS_REPORTLAB, reason="reportlab не установлен")
def test_empty_rows_still_creates_pdf(tmp_path):
    out = tmp_path / "empty_rows.pdf"
    res = generate_pdf_report([], str(out), "2026-09-01")
    assert out.exists() and out.stat().st_size > 0


# ---------------------------------------------------------------------------
# Чистые функции (не требуют reportlab) — статистика/формат/CoT-фильтр
# ---------------------------------------------------------------------------

def test_format_report_date_formats_dd_mm_yyyy():
    assert format_report_date("2026-09-01") == "01.09.2026"
    assert format_report_date(datetime(2026, 9, 1, 12, 0)) == "01.09.2026"
    with pytest.raises(ValueError):
        format_report_date(None)
    with pytest.raises(ValueError):
        format_report_date("")


def test_build_statistics_counts_and_labels():
    rows = [
        _row(dialog_type="buy", is_sale=True),
        _row(dialog_type="buy", is_sale=False),
        _row(dialog_type="service"),
        _row(dialog_type="corporate"),
    ]
    stats = build_statistics(rows)
    assert stats["total"] == 4
    assert stats["with_sale"] == 1
    assert stats["counts"]["продажи"] == 2
    assert stats["counts"]["сервисные обращения"] == 1
    assert stats["counts"]["корпоративные"] == 1
    assert stats["counts"]["прочие"] == 0


def test_build_statistics_empty():
    stats = build_statistics([])
    assert stats["total"] == 0
    assert stats["with_sale"] == 0
    assert stats["percent"]["продажи"] == 0.0


def test_safe_reason_keeps_short_business_reason():
    row = {"model_reasoning": "Клиент купил АКБ, оплата картой, покупка подтверждена."}
    out = safe_classification_reason(row)
    assert out == row["model_reasoning"]


def test_safe_reason_filters_long_text_as_cot():
    # очень длинный текст -> скорее CoT -> заменяется на безопасный fallback
    row = {"model_reasoning": "Пошаговое объяснение " * 80}
    out = safe_classification_reason(row)
    assert out != row["model_reasoning"]
    assert out  # не пустой fallback


def test_safe_reason_none_returns_fallback():
    out = safe_classification_reason({"model_reasoning": None})
    assert out and len(out) > 10
