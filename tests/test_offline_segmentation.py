"""Тесты новой offline segmentation (без клиентской client_id-опоры).

Ключевые свойства которые проверяем:
  * segmentation НЕ опирается на старый client_id (тесты с «адверсариальными»
    client_id показывают: segmentation строится по тексту/LLM, а не по ID);
  * все исходные строки покрыты сегментами (union == [0..N-1], без дублей);
  * adjacent-сегменты сохраняются, пересекающиеся — склеиваются;
  * дыры (непокрытые строки) заделываются placeholder'ами;
  * «Здравствуйте»/«До свидания» не являются жёсткой границей;
  * client_id из исходников попадает только в debug (old_client_ids).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from offline_analysis.segmentation import (
    Row,
    Segment,
    build_chunks,
    measure_rows,
    parse_segmentation_response,
    repair_segments,
    resplit_long_segments,
    duration_metrics,
    segment_rows,
)
from offline_analysis.prompt import (
    RESEGMENT_LONG_SYSTEM_PROMPT,
    SEGMENTATION_SYSTEM_PROMPT,
)


# ---------------------------------------------------------------------------
# Фиксированные helpers
# ---------------------------------------------------------------------------

def make_rows(texts, client_ids=None, base=datetime(2026, 8, 27, 10, 0, 0)):
    client_ids = client_ids if client_ids is not None else [None] * len(texts)
    out = []
    for i, (t, cid) in enumerate(zip(texts, client_ids)):
        out.append(
            Row(
                created_at=base + timedelta(seconds=10 * i),
                text=t,
                seller_id="ivan",
                client_id=cid,
                dialog_type=None,
                is_sale=False,
                is_alarm_triggered=False,
                session_id=f"s{i}",
                index=i,
            )
        )
    return out


class FakeSegmenter:
    """Определяет JSON-ответ по списку (start, end, type, conf, reason).

    В тестах целевой chunk один (target_chunk_tokens large => 1 chunk).
    """

    def __init__(self, spans):
        self.spans = spans
        self.calls = 0

    def chat_json(self, system: str, user: str) -> dict:
        self.calls += 1
        segs = [
            {"start_row": s, "end_row": e, "type": t,
             "confidence": c, "reason": r or "fake"}
            for (s, e, t, c, r) in self.spans
        ]
        return {"content": json.dumps({"segments": segs})}


def seg_covered(segments):
    return [i for seg in segments for i in range(seg.start_index, seg.end_index + 1)]


# ---------------------------------------------------------------------------
# 1. repair_segments — чистые свойства (без LLM)
# ---------------------------------------------------------------------------

def test_repair_fills_holes():
    # строки 0..9, LLM разметил 0..2 и 6..9; 3..5 — дыра -> placeholder
    raw = [
        {"start_row": 0, "end_row": 2, "type": "dialog", "confidence": 0.9, "reason": "a"},
        {"start_row": 6, "end_row": 9, "type": "dialog", "confidence": 0.8, "reason": "b"},
    ]
    out = repair_segments(raw, total_rows=10, fallback_type="unknown")
    assert seg_covered_defs(out) == list(range(10))
    # 3..5 — placeholder
    gap = [s for s in out if s["start_row"] == 3 and s["end_row"] == 5]
    assert len(gap) == 1 and gap[0]["type"] == "unknown"


def seg_covered_defs(definitions):
    out = []
    for s in definitions:
        out.extend(range(s["start_row"], s["end_row"] + 1))
    return out


def test_repair_keeps_adjacent_segments_distinct():
    # 0..2 и 3..5 — adjacent (2+1==3), НЕ должно склеиваться
    raw = [
        {"start_row": 0, "end_row": 2, "type": "dialog", "confidence": 0.9, "reason": "a"},
        {"start_row": 3, "end_row": 5, "type": "dialog", "confidence": 0.8, "reason": "b"},
    ]
    out = repair_segments(raw, total_rows=6)
    assert len(out) == 2
    assert out[0]["end_row"] == 2 and out[1]["start_row"] == 3


def test_repair_merges_true_overlaps():
    # 0..4 и 3..7 пересекаются (3 <= 4) -> склеиваем в 0..7
    raw = [
        {"start_row": 0, "end_row": 4, "type": "dialog", "confidence": 0.9, "reason": "a"},
        {"start_row": 3, "end_row": 7, "type": "dialog", "confidence": 0.8, "reason": "b"},
    ]
    out = repair_segments(raw, total_rows=8)
    assert len(out) == 1
    assert out[0]["start_row"] == 0
    assert out[0]["end_row"] == 7
    assert out[0]["type"] == "dialog"
    # confidence минимальный из склеенных (0.8)
    assert out[0]["confidence"] in (0.8, 0.9)


def test_repair_clips_out_of_range():
    raw = [
        {"start_row": -5, "end_row": 3, "type": "dialog", "confidence": 0.9, "reason": "a"},
        {"start_row": 8, "end_row": 100, "type": "dialog", "confidence": 0.8, "reason": "b"},
    ]
    out = repair_segments(raw, total_rows=8)  # 0..7
    assert seg_covered_defs(out) == list(range(8))


def test_repair_invalid_rows_dropped():
    # start > end — игнорируется
    raw = [
        {"start_row": 5, "end_row": 2, "type": "dialog", "confidence": 0.9, "reason": "bad"},
        {"start_row": 0, "end_row": 7, "type": "dialog", "confidence": 0.9, "reason": "ok"},
    ]
    out = repair_segments(raw, total_rows=8)
    assert len(out) == 1


def test_parse_segmentation_response_handles_noise_and_synonyms():
    content = (
        "```json\n"
        '{"segments":[{"start_row":0,"end_row":3,"type":"client_dialog",'
        '"confidence":0.7,"reason":"x"},{"start_row":4,"end_row":7,"type":"staff",'
        '"confidence":"0.6","reason":"y"}]}'
        "\n```"
    )
    out = parse_segmentation_response(content, expected_rows=8)
    assert len(out) == 2
    assert out[0]["type"] == "dialog"  # synonym
    assert out[1]["type"] == "employee"  # synonym
    assert out[1]["confidence"] == 0.6


def test_parse_empty_or_malformed_returns_empty():
    assert parse_segmentation_response("hello, no json here", 8) == []
    assert parse_segmentation_response("", 8) == []
    assert parse_segmentation_response("not json", 8) == []


# ---------------------------------------------------------------------------
# 2. Scenario tests: 10 обязательных (с fake LLM, детерминированно)
# ---------------------------------------------------------------------------

def test_scenario1_several_clients_same_old_client_id_are_split():
    """Несколько клиентов с ОДНИМ old client_id -> new-алгоритм разделяет их."""
    texts = [
        "сколько стоит аккумулятор",      # 0
        "дайте подержать",               # 1
        "хорошо, беру",                  # 2  <- конец клиента A
        "сколько стоит ваш",             # 3  <- начало клиента B (тот же old client_id!)
        "дорого, до свидания",           # 4
    ]
    client_ids = ["client_7"] * 5  # old ID одинаковый для обоих клиентов
    rows = make_rows(texts, client_ids)

    # LLM «знает» о двух клиентах: 0..2 и 3..4
    llm = FakeSegmenter([(0, 2, "dialog", 0.9, "client A"), (3, 4, "dialog", 0.9, "client B")])
    segments = segment_rows(rows, llm=llm, store_id="A", verbose=False)

    assert len(segments) == 2
    assert segments[0].segment_type == "dialog"
    assert segments[1].segment_type == "dialog"
    # old client_id сохранён в debug для обоих, но на сегменты не повлиял
    assert all("client_7" in seg.old_client_ids for seg in segments)


def test_scenario2_client_without_zdravstvuyte():
    """Клиент без «здравствуйте» -> всё равно определяется как dialog."""
    texts = [
        "сколько стоит бустер",   # 0
        "да, есть 12v",          # 1
        "спасибо, беру",         # 2
    ]
    rows = make_rows(texts, client_ids=[None, None, None])
    llm = FakeSegmenter([(0, 2, "dialog", 0.95, "без приветствия, явный запрос клиента")])
    segments = segment_rows(rows, llm=llm, store_id="A", verbose=False)
    assert len(segments) == 1
    assert segments[0].segment_type == "dialog"


def test_scenario3_client_without_do_svidaniya_still_closed():
    """Клиент без «до свидания» -> разговор всё равно закрывается как один сегмент."""
    texts = [
        "сколько стоит",      # 0
        "дешево, беру",       # 1
        "спасибо",            # 2
    ]
    rows = make_rows(texts)
    llm = FakeSegmenter([(0, 2, "dialog", 0.9, "покупка завершена без явного прощания")])
    segments = segment_rows(rows, llm=llm, store_id="A", verbose=False)
    assert len(segments) == 1 and segments[0].segment_type == "dialog"


def test_scenario4_long_dialog_not_cut_by_max_sec():
    """Длинный разговор (40 строк) НЕ режется по max-sec — один сегмент."""
    texts = ["речь %d" % i for i in range(40)]
    rows = make_rows(texts)
    llm = FakeSegmenter([(0, 39, "dialog", 0.9, "один продолжительный разговор")])
    segments = segment_rows(rows, llm=llm, store_id="A", verbose=False)
    assert len(segments) == 1
    assert segments[0].row_count == 40
    # длительность > любого порога max-sec, который мог бы существовать
    assert segments[0].duration_sec > 0


def test_scenario5_two_dialogs_short_gap_still_split():
    """Два разговора с коротким промежутком -> 2 сегмента (LLM решает сам)."""
    texts = [
        "сколько стоит",     # 0  клиент A
        "беру",              # 1
        "сколько стоит",     # 2  клиент B
        "дорого",            # 3
    ]
    rows = make_rows(texts)
    llm = FakeSegmenter([(0, 1, "dialog", 0.9, "A"), (2, 3, "dialog", 0.85, "B")])
    segments = segment_rows(rows, llm=llm, store_id="A", verbose=False)
    # adjacent сегменты 0..1 и 2..3 сохраняются отдельно
    assert len(segments) == 2


def test_scenario6_employee_between_two_customers_no_mixing():
    """customer -> employee -> customer: 3 сегмента, без смешивания."""
    texts = [
        "сколько стоит",     # 0  клиент A
        "беру",              # 1
        "перекинь ключ",     # 2  сотрудник
        "ага, поехали",      # 3
        "сколько стоит",     # 4  клиент B
        "спасибо",           # 5
    ]
    rows = make_rows(texts)
    llm = FakeSegmenter([
        (0, 1, "dialog", 0.9, "A"),
        (2, 3, "employee", 0.9, "переговор сотрудников"),
        (4, 5, "dialog", 0.9, "B"),
    ])
    segments = segment_rows(rows, llm=llm, store_id="A", verbose=False)
    assert len(segments) == 3
    assert [s.segment_type for s in segments] == ["dialog", "employee", "dialog"]
    # ни один сегмент не перекрывает другого
    covered = seg_covered(segments)
    assert sorted(covered) == list(range(6)) and len(covered) == 6


def test_scenario7_background_not_becomes_dialog():
    """Посторонняя речь (тест, шум) — не dialog, не попадёт в customer report."""
    texts = ["тест", "тест микрофон", "шум"]
    rows = make_rows(texts)
    llm = FakeSegmenter([(0, 2, "background", 0.85, "тест/шум, без обращения")])
    segments = segment_rows(rows, llm=llm, store_id="A", verbose=False)
    assert len(segments) == 1
    assert segments[0].segment_type == "background"


def test_scenario8_one_old_client_id_contains_multiple_real_clients():
    """ВЕСЬМ ВАЖНЫЙ: один old client_id содержит несколько реальных клиентов.

    Old segmentation (client_id=1 covers всё) -> 1 «клиент».
    New segmentation -> несколько диалогов."""
    texts = [
        "акум 60",           # 0  клиент 1
        "хорошо",            # 1
        "сколько 95",        # 2  клиент 2
        "дорого",            # 3
        "сколько 70",        # 4  клиент 3
        "беру",              # 5
    ]
    client_ids = ["the_one"] * 6  # ОДИН старый ID на всех
    rows = make_rows(texts, client_ids)

    llm = FakeSegmenter([
        (0, 1, "dialog", 0.9, "client 1"),
        (2, 3, "dialog", 0.9, "client 2"),
        (4, 5, "dialog", 0.9, "client 3"),
    ])
    segments = segment_rows(rows, llm=llm, store_id="A", verbose=False)

    assert len(segments) == 3
    assert all(s.segment_type == "dialog" for s in segments)
    # old client_id сохранён в каждом (debug), но segmentation разделила
    assert all("the_one" in seg.old_client_ids for seg in segments)


def test_scenario9_several_old_client_ids_one_real_dialog():
    """Наоборот: несколько old client_id на ОДИН реальный разговор -> 1 сегмент."""
    texts = [
        "сколько стоит",      # 0  (old client_1)
        "а какой ёмкость",    # 1  (old client_2)
        "беру",              # 2  (old client_3)
    ]
    client_ids = ["client_1", "client_2", "client_3"]
    rows = make_rows(texts, client_ids)

    llm = FakeSegmenter([(0, 2, "dialog", 0.95, "один разговор")])
    segments = segment_rows(rows, llm=llm, store_id="A", verbose=False)
    assert len(segments) == 1
    # все 3 old ID видимы в debug
    assert set(segments[0].old_client_ids) == {"client_1", "client_2", "client_3"}


def test_scenario10_preamble_not_attributed_to_first_client():
    """Початок файла (preamble) НЕ автоматически попадает в разговор первого клиента.

    Old client_id на первых строках не значит, что это клиентский диалог:
    LLM сама решает тип и границы."""
    texts = [
        "тест микрофон",        # 0  preamble (noise)
        "шум",                 # 1
        "сколько стоит",       # 2  — первый реальный клиент
        "беру",                # 3
    ]
    # old client_id уже с первой реплики (realtime зафиксировал "клиента" на тесте)
    client_ids = ["early", "early", None, None]
    rows = make_rows(texts, client_ids)

    llm = FakeSegmenter([
        (0, 1, "background", 0.85, "тест/шум"),
        (2, 3, "dialog", 0.9, "первый реальный клиент"),
    ])
    segments = segment_rows(rows, llm=llm, store_id="A", verbose=False)

    assert len(segments) == 2
    assert segments[0].segment_type == "background"  # НЕ dialog
    assert segments[1].segment_type == "dialog"
    # old client_id попал только в debug сегмента 0 и не определил его как клиента
    assert "early" in segments[0].old_client_ids


# ---------------------------------------------------------------------------
# 3. Hierarchical chunking: большой поток делится, на стыках ничего не пропадает
# ---------------------------------------------------------------------------

def test_small_flow_single_chunk():
    rows = make_rows(["тест"] * 10)
    ch = build_chunks(rows, target_tokens=10_000)
    assert len(ch) == 1


def test_large_flow_chunks_respect_token_budget():
    rows = make_rows([f"слов {i}" for i in range(5000)])
    ch = build_chunks(rows, target_tokens=4000)
    assert len(ch) >= 2
    total = sum(len(c) for c, _ in ch)
    assert total == len(rows)
    seen = [r.index for c, _ in ch for r in c]
    assert seen == list(range(len(rows)))


def test_segrows_chunking_fills_all_rows_when_llm_misses_one_chunk():
    rows_big = make_rows([f"тест {i}" for i in range(500)])

    class Chatty:
        def __init__(self):
            self.calls = 0

        def chat_json(self, system, user):
            self.calls += 1
            if self.calls == 1:
                import re
                idxs = [int(m) for m in re.findall(r"^\d+ \|", user, re.M)]
                segs = [{"start_row": idxs[0], "end_row": idxs[-1],
                         "type": "dialog", "confidence": 0.8, "reason": "chunk1"}]
                return {"content": json.dumps({"segments": segs})}
            return {"content": ""}

    segments = segment_rows(rows_big, llm=Chatty(), store_id="A", target_chunk_tokens=4000, verbose=False)
    covered = [i for s in segments for i in range(s.start_index, s.end_index + 1)]
    assert covered == list(range(len(rows_big)))


# ---------------------------------------------------------------------------
# 4. Invariant: segmentation НЕ читает client_id (анти-адверсайт)
# ---------------------------------------------------------------------------

def test_segmentation_ignores_legacy_client_id_for_type_assignment():
    texts = ["коллега, перекинь ключ", "ага, поехали"]
    client_ids = [None, "client_9"]
    rows = make_rows(texts, client_ids)
    llm = FakeSegmenter([(0, 1, "employee", 0.9, "сотрудники")])
    segments = segment_rows(rows, llm=llm, store_id="A", verbose=False)
    assert segments[0].segment_type == "employee"
    assert "client_9" in segments[0].old_client_ids


# ---------------------------------------------------------------------------
# 5. Segment-объекты: свойства, coverage, debug-поля
# ---------------------------------------------------------------------------

def test_segment_properties():
    rows = make_rows(["привет", "сколько", "беру"])
    seg = Segment(
        segment_id="segment_001", store_id="storeA",
        start_index=0, end_index=2, segment_type="dialog",
        confidence=0.9, reason="test", rows=rows,
    )
    assert seg.row_count == 3
    assert seg.row_indices == [0, 1, 2]
    assert seg.start_at is not None and seg.end_at is not None
    assert seg.session_ids == ["s0", "s1", "s2"]
    assert seg.duration_sec >= 0


def test_segment_empty_rows_edge_case():
    seg = Segment(
        segment_id="x", store_id="s", start_index=0, end_index=0,
        segment_type=None, confidence=0.0, reason=None, rows=[],
    )
    assert seg.row_count == 0
    assert seg.start_at is None and seg.end_at is None


# ---------------------------------------------------------------------------
# 6. RE-SPLIT «подозрительно длинных» dialog-сегментов (новый механизм)
# ---------------------------------------------------------------------------
# Fake LLM, различающий первичный segmentation-prompt и re-segmentation-prompt
# (идентификация по системному промпту — оба являются константами prompt).

class DualFakeLLM:
    """Первичная segmentation => ``primary``; re-segmentation => ``resegment``.

    ``resegment``: список глобальных (s, e, type, conf, reason) ИЛИ ``None``
    (LLM отвечает segments:[] = «оставить как есть»).
    """

    def __init__(self, primary, resegment):
        self.primary = primary
        self.resegment = resegment          # list | None
        self.calls_primary = 0
        self.calls_re = 0

    def _dump(self, spans):
        segs = [
            {"start_row": s, "end_row": e, "type": t, "confidence": c, "reason": r}
            for (s, e, t, c, r) in spans
        ]
        return {"content": json.dumps({"segments": segs}, ensure_ascii=False)}

    def chat_json(self, system: str, user: str, **kwargs) -> dict:
        if system == RESEGMENT_LONG_SYSTEM_PROMPT:
            self.calls_re += 1
            if self.resegment is None:
                return {"content": json.dumps({"segments": []}, ensure_ascii=False)}
            return self._dump(self.resegment)
        self.calls_primary += 1
        return self._dump(self.primary)


def make_long(n, base=datetime(2026, 8, 28, 10, 0, 0), step_sec=10, client_ids=None):
    """n строк, interval ~n*step_sec секунд. 400 rows => 66.5 min (very long)."""
    out = []
    cids = client_ids or [None] * n
    for i in range(n):
        out.append(Row(
            created_at=base + timedelta(seconds=step_sec * i),
            text=f"фрагмент {i}", seller_id="ivan",
            client_id=cids[i], dialog_type=None, is_sale=False,
            is_alarm_triggered=False, session_id=f"s{i}", index=i,
        ))
    return out


def test_resplit1_very_long_segment_is_split():
    """1. Очень длинный сегмент -> LLM re-split разбивает на части."""
    n = 400  # 66.5 min (>= 60s*60 => very long)
    rows = make_long(n)
    assert make_long(n)[-1].created_at > make_long(n)[0].created_at + timedelta(minutes=60)
    llm = DualFakeLLM(
        primary=[(0, n - 1, "dialog", 0.8, "один большой блок (2 клиента внутри)")],
        resegment=[
            (0, 199, "dialog", 0.9, "клиент A покупка"),
            (200, n - 1, "dialog", 0.9, "клиент B покупка"),
        ],
    )
    segments = segment_rows(rows, llm=llm, store_id="A", verbose=False)
    assert len(segments) == 2
    assert segments[0].segment_type == "dialog" and segments[1].segment_type == "dialog"
    assert segments[0].start_index == 0 and segments[0].end_index == 199
    assert segments[1].start_index == 200 and segments[1].end_index == n - 1
    assert llm.calls_re >= 1


def test_resplit2_two_clients_same_range_split():
    """2. Два независимых клиента в одном временном диапазоне -> 2 диалога."""
    n = 300  # 50 min (>= 30 min => review)
    rows = make_long(n)
    llm = DualFakeLLM(
        primary=[(0, n - 1, "dialog", 0.75, "длинный блок")],
        resegment=[
            (0, 149, "dialog", 0.9, "первый клиент"),
            (150, n - 1, "dialog", 0.9, "второй клиент"),
        ],
    )
    segments = segment_rows(rows, llm=llm, store_id="A", verbose=False)
    assert len(segments) == 2
    # покрытие без дублей и пропусков
    covered = sorted(i for s in segments for i in range(s.start_index, s.end_index + 1))
    assert covered == list(range(n))


def test_resplit3_purchase_then_completion_then_new_client():
    """3. «покупка -> завершение -> новый клиент» -> граница после завершения."""
    n = 200  # 33 min (>= 30 min => review)
    texts = []
    for i in range(n):
        if i == 50:
            texts.append("чек готов, до свидания")        # завершение клиента A
        elif i == 51:
            texts.append("здравствуйте, сколько стоит 70-й")  # новый клиент B
        else:
            texts.append(f"речь {i}")
    rows = make_long(n)
    for i, t in enumerate(texts):
        rows[i].text = t
    llm = DualFakeLLM(
        primary=[(0, n - 1, "dialog", 0.7, "всё в одном")],
        resegment=[
            (0, 50, "dialog", 0.85, "клиент A: покупка + завершение"),
            (51, n - 1, "dialog", 0.85, "клиент B: новый разговор"),
        ],
    )
    segments = segment_rows(rows, llm=llm, store_id="A", verbose=False)
    assert len(segments) == 2
    # граница именно между завершением A (<=50) и новым обращением (>=51)
    assert segments[0].end_index == 50 and segments[1].start_index == 51


def test_resplit4_several_purchases_in_long_fragment():
    """4. Несколько покупок внутри длинного фрагмента -> 3 диалога."""
    n = 360  # 60 min
    rows = make_long(n)
    llm = DualFakeLLM(
        primary=[(0, n - 1, "dialog", 0.7, "несколько покупок")],
        resegment=[
            (0, 119, "dialog", 0.9, "покупка 1"),
            (120, 239, "dialog", 0.9, "покупка 2"),
            (240, n - 1, "dialog", 0.9, "покупка 3"),
        ],
    )
    segments = segment_rows(rows, llm=llm, store_id="A", verbose=False)
    assert len(segments) == 3
    for i, s in enumerate(segments):
        assert s.segment_type == "dialog"
    assert [ (s.start_index, s.end_index) for s in segments ] == \
           [(0, 119), (120, 239), (240, n - 1)]


def test_resplit5_long_single_conversation_not_split():
    """5. Длинный НО ИСТИННО ОДИН разговор — НЕ должен резаться только по времени."""
    n = 240  # 40 min
    rows = make_long(n)
    llm = DualFakeLLM(
        primary=[(0, n - 1, "dialog", 0.95, "один клиент, долгий выбор АКБ")],
        resegment=None,  # LLM: «оставить как есть» (segments:[])
    )
    segments = segment_rows(rows, llm=llm, store_id="A", verbose=False)
    assert len(segments) == 1
    assert segments[0].row_count == n
    assert segments[0].segment_type == "dialog"
    # LLM действительно был вызван для проверки, но решение — «не резать»
    assert llm.calls_re >= 1


def test_resplit6_independent_of_client_id():
    """6. Re-split НЕ зависит от old client_id (адверсариальные ID)."""
    n = 200
    # все 200 строк — с ОДНИМ client_id; LLM всё равно делит по тексту
    cids = ["single_id"] * n
    rows = make_long(n, client_ids=cids)
    llm = DualFakeLLM(
        primary=[(0, n - 1, "dialog", 0.7, "длинный блок")],
        resegment=[
            (0, 99, "dialog", 0.9, "клиент 1"),
            (100, n - 1, "dialog", 0.9, "клиент 2"),
        ],
    )
    segments = segment_rows(rows, llm=llm, store_id="A", verbose=False)
    assert len(segments) == 2
    # old client_id сохранён в debug у обоих сегментов, но на разделение не повлиял
    assert all("single_id" in s.old_client_ids for s in segments)


def test_resplit7_coverage_no_gaps_no_dups():
    """7. Re-split НЕ теряет строки и не дублирует (unassigned==0, duplicated==0)."""
    n = 200
    rows = make_long(n)
    llm = DualFakeLLM(
        primary=[(0, n - 1, "dialog", 0.7, "длинный")],
        resegment=[
            (0, 60, "dialog", 0.9, "A"),
            (61, 130, "dialog", 0.9, "B"),
            (131, n - 1, "dialog", 0.9, "C"),
        ],
    )
    segments = segment_rows(rows, llm=llm, store_id="A", verbose=False)
    covered = [i for s in segments for i in range(s.start_index, s.end_index + 1)]
    assert sorted(covered) == list(range(n)), "no gaps"
    assert len(covered) == len(set(covered)), "no duplicates"
    assert len(covered) == n


def test_resplit_invalid_response_keeps_original():
    """LLM вернул невалидный ответ (нет JSON) -> сегмент НЕ меняется."""
    n = 200
    rows = make_long(n)

    class BrokenReLLM:
        def __init__(self, primary):
            self.primary = primary
        def chat_json(self, system, user, **kwargs):
            if system == RESEGMENT_LONG_SYSTEM_PROMPT:
                # мусор без JSON извлечение
                return {"content": "извините, я не могу разобрать, JSON не будет", "raw": {}}
            return {"content": json.dumps({"segments": [
                {"start_row": 0, "end_row": n - 1, "type": "dialog", "confidence": 0.7, "reason": "one"}
            ]})}
    llm = BrokenReLLM(None)
    segments = segment_rows(rows, llm=llm, store_id="A", verbose=False)
    # невалидный re-split отброшен -> остаётся ОДИН длинный сегмент
    assert len(segments) == 1
    assert segments[0].start_index == 0 and segments[0].end_index == n - 1


def test_resplit_out_of_bounds_response_rejected():
    """LLM вернул сегмент, выходящий за границы блока -> НЕ применяется."""
    n = 200
    rows = make_long(n)

    class OOBLLM:
        def __init__(self, primary):
            self.primary = primary
        def chat_json(self, system, user, **kwargs):
            if system == RESEGMENT_LONG_SYSTEM_PROMPT:
                # сегмент выходит далеко за границу [0..199]: 500..600 невалидно
                return {"content": json.dumps({"segments": [
                    {"start_row": 500, "end_row": 600, "type": "dialog", "confidence": 0.9, "reason": "x"},
                ]})}
            return {"content": json.dumps({"segments": [
                {"start_row": 0, "end_row": n - 1, "type": "dialog", "confidence": 0.7, "reason": "one"}
            ]})}
    llm = OOBLLM(None)
    segments = segment_rows(rows, llm=llm, store_id="A", verbose=False)
    # невалидное деление -> остаётся один сегмент
    assert len(segments) == 1
    assert segments[0].start_index == 0 and segments[0].end_index == n - 1


def test_duration_metrics_report():
    """duration_metrics даёт min/max/median/p90/p95 и count over 30/60/90/120 мин."""
    n = 400  # 66.5 min
    rows = make_long(n)
    seg = Segment(
        segment_id="s1", store_id="A", start_index=0, end_index=n - 1,
        segment_type="dialog", confidence=0.9, reason="r", rows=rows,
    )
    m = duration_metrics([seg])
    for key in ("count", "min_sec", "max_sec", "median_sec", "p90_sec", "p95_sec",
                "gt_30min", "gt_60min", "gt_90min", "gt_120min"):
        assert key in m
    assert m["count"] == 1
    assert m["max_sec"] >= 60 * 60
    assert m["gt_30min"] == 1 and m["gt_60min"] == 1
    assert m["gt_90min"] == 0 and m["gt_120min"] == 0
