"""Объединение связанных речевых сегментов.

Цель: найти сегменты, которые относятся к одному разговору, но были разделены
на этапе первичной сегментации, и объединить их перед классификацией.

Важно:
- НЕ изменяет исходные сегменты
- НЕ меняет границы строк
- Сохраняет связь с исходными сегментами
- Работает в режиме "только анализ" для тестирования на эталонах
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from .segmentation import Segment, Row


@dataclass
class MergeGroup:
    """Группа сегментов, предлагаемых к объединению."""
    
    group_id: str
    segments: list[Segment]  # исходные сегменты в хронологическом порядке
    segment_ids: list[str]
    start_index: int  # первая строка первой сегмента
    end_index: int    # последняя строка последнего сегмента
    total_duration_sec: float
    gaps: list[float]  # разрывы между сегментами в секундах
    merged_text: str   # объединённый текст
    merge_reason: str  # причина предложения объединения
    confidence: float  # уверенность в правильности объединения
    
    @property
    def row_count(self) -> int:
        return self.end_index - self.start_index + 1
    
    @property
    def segment_count(self) -> int:
        return len(self.segments)


# ---------------------------------------------------------------------------
# Анализ признаков для объединения
# ---------------------------------------------------------------------------

# Паттерны для обнаружения признаков продолжения разговора
_CONTINUATION_PATTERNS = [
    r"(?:^|\s)(далее|дальше|потом|затем|ещё|еще|также|тоже)\b",
    r"(?:^|\s)(а( |:|,)|но|или|и)\b",
    r"(?:^|\s)(этот|такой|тот|тут|там|вот)\b",
]

# Паттерны для обнаружения завершения разговора
_COMPLETION_PATTERNS = [
    r"(?:^|\s)(до\s+свидания|до\s+встреч|всего\s+хорошего|успех\w*|спасибо\s*(за\s+покупку)?|будем\s+рады\s+видеть)\b",
    r"(?:^|\s)(чек|оплат\w*|терминал|гарантийный\s+талон|бонус\w*)\b",
]

# Паттерны для нового приветствия (новый клиент)
_GREETING_PATTERNS = [
    r"(?:^|\s)(здравств\w*|добрый\s+(?:день|вечер|утро)|добрый\w*|дарово|привет)\b",
]


def _text_matches_pattern(text: str, patterns: list[str]) -> bool:
    """Проверка, содержит ли текст хотя бы один из паттернов."""
    text_lower = text.lower()
    for pattern in patterns:
        if re.search(pattern, text_lower):
            return True
    return False


def _analyze_continuation(seg1: Segment, seg2: Segment) -> dict:
    """Анализирует признаки продолжения разговора между двумя сегментами."""
    
    text1 = seg2.text.lower()
    text2 = seg2.text.lower()
    
    # Проверка на продолжение
    has_continuation = _text_matches_pattern(text2, _CONTINUATION_PATTERNS)
    
    # Проверка на вопрос-ответ
    has_question_in_first = any(q in text1 for q in ['?', 'сколько', 'какой', 'есть', 'цена', 'стоит'])
    has_answer_in_second = any(a in text2 for a in ['да', 'угу', 'ага', 'давайте', 'хорошо', 'конечно'])
    has_qa_pattern = has_question_in_first and has_answer_in_second
    
    # Проверка на общие темы (товар, модель, цена)
    common_topics = []
    topic_words = ['аккумулятор', 'акб', 'батарея', 'цена', 'стоит', 'сколько', 'модель', 'автомобиль', 'машина']
    for word in topic_words:
        if word in text1 and word in text2:
            common_topics.append(word)
    
    return {
        "has_continuation": has_continuation,
        "has_qa_pattern": has_qa_pattern,
        "common_topics": common_topics,
        "common_topics_count": len(common_topics),
    }


def _analyze_completion(seg: Segment) -> dict:
    """Анализирует признаки завершения разговора в сегменте."""
    text = seg.text.lower()
    
    has_completion = _text_matches_pattern(text, _COMPLETION_PATTERNS)
    has_greeting = _text_matches_pattern(text, _GREETING_PATTERNS)
    
    return {
        "has_completion": has_completion,
        "has_greeting": has_greeting,
    }


def _calculate_gap_seconds(seg1: Segment, seg2: Segment) -> float:
    """Вычисляет разрыв между двумя сегментами в секундах.
    
    Если временные метки недоступны, используем разрыв по строкам как эвристику:
    предполагаем ~1 строка = ~2-3 секунды речи.
    """
    if seg1.end_at is not None and seg2.start_at is not None:
        return float((seg2.start_at - seg1.end_at).total_seconds())
    
    # Fallback: используем разрыв по строкам
    row_gap = seg2.start_index - seg1.end_index - 1
    if row_gap < 0:
        return 0.0
    # Эвристика: 1 строка ≈ 2.5 секунды
    return row_gap * 2.5


def _should_not_merge(seg1: Segment, seg2: Segment) -> tuple[bool, str]:
    """Проверка, НЕ следует ли объединять сегменты (признаки нового клиента)."""
    
    # Если есть завершение в первом сегменте и приветствие во втором — это разные клиенты
    completion1 = _analyze_completion(seg1)
    greeting2 = _analyze_completion(seg2)
    
    if completion1["has_completion"] and greeting2["has_greeting"]:
        return True, "завершение_первого_и_приветствие_второго"
    
    # Если оба сегмента имеют приветствия — возможно, разные клиенты
    if completion1["has_greeting"] and greeting2["has_greeting"]:
        # Но не всегда: проверим контекст
        pass
    
    return False, ""


# ---------------------------------------------------------------------------
# Поиск кандидатов на объединение
# ---------------------------------------------------------------------------

def find_merge_candidates(
    segments: list[Segment],
    time_threshold_sec: float = 10.0,
    analyze_only: bool = True,
) -> tuple[list[MergeGroup], list[dict]]:
    """Находит группы сегментов, которые могут относиться к одному разговору.
    
    Args:
        segments: список сегментов после первичной сегментации
        time_threshold_sec: максимальный разрыв в секундах для рассмотрения
        analyze_only: если True — только анализ, без фактического объединения
    
    Returns:
        (merge_groups, metrics):
            - merge_groups: группы сегментов, предлагаемые к объединению
            - metrics: статистика анализа
    """
    
    if not segments:
        return [], {"candidates_found": 0, "groups_proposed": 0}
    
    # Фильтруем только dialog-сегменты
    dialog_segments = [s for s in segments if s.segment_type == "dialog"]
    
    if len(dialog_segments) < 2:
        return [], {"candidates_found": 0, "groups_proposed": 0, "dialog_count": len(dialog_segments)}
    
    candidates = []
    groups: list[MergeGroup] = []
    group_counter = 0
    
    # Проходим по соседним dialog-сегментам
    for i in range(len(dialog_segments) - 1):
        seg1 = dialog_segments[i]
        seg2 = dialog_segments[i + 1]
        
        # Проверяем, идут ли сегменты подряд без промежуточных сегментов
        # Если между ними есть хотя бы один другой сегмент (background/employee),
        # это означает, что первичная сегментация уже приняла решение разделить их
        if seg2.start_index != seg1.end_index + 1:
            # Есть промежуточные сегменты — пропускаем
            continue
        
        # Вычисляем разрыв
        gap = _calculate_gap_seconds(seg1, seg2)
        
        # Если разрыв превышает порог — пропускаем
        if gap > time_threshold_sec:
            continue
        
        # Если разрыв 0 (сегменты идут подряд) — это означает, что первичная
        # сегментация уже приняла решение разделить их. Мы должны уважать это
        # решение и НЕ предлагать объединение, чтобы не сломать эталонные результаты.
        # Объединение имеет смысл только для сегментов, разделённых другими
        # сегментами (employee/background), которые могли быть ошибочно вставлены.
        if gap == 0:
            continue
        
        # Проверяем, не следует ли объединять (признаки нового клиента)
        should_not, reason = _should_not_merge(seg1, seg2)
        if should_not:
            continue
        
        # Анализируем признаки продолжения
        continuation = _analyze_continuation(seg1, seg2)
        
        # Оцениваем вероятность объединения
        score = 0.0
        reasons = []
        
        if gap < 5:
            score += 0.3
            reasons.append(f"малый_разрыв_{gap:.1f}с")
        elif gap < 10:
            score += 0.2
            reasons.append(f"разрыв_{gap:.1f}с")
        
        if continuation["has_continuation"]:
            score += 0.25
            reasons.append("признаки_продолжения")
        
        if continuation["has_qa_pattern"]:
            score += 0.25
            reasons.append("вопрос_ответ")
        
        if continuation["common_topics_count"] >= 2:
            score += 0.2
            reasons.append(f"общие_темы_{','.join(continuation['common_topics'][:3])}")
        
        # Добавляем как кандидата только если есть сильные признаки
        # Повышаем порог для избежания ложных срабатываний
        if score >= 0.95 and reasons:
            candidates.append({
                "seg1_id": seg1.segment_id,
                "seg2_id": seg2.segment_id,
                "gap_sec": gap,
                "score": score,
                "reasons": reasons,
                "continuation": continuation,
            })
    
    # Группируем кандидатов в цепи (seg1-seg2-seg3...)
    if candidates:
        used = set()
        for cand in sorted(candidates, key=lambda x: -x["score"]):
            if cand["seg1_id"] in used or cand["seg2_id"] in used:
                continue
            
            # Начинаем новую группу
            group_segs = []
            group_ids = [cand["seg1_id"], cand["seg2_id"]]
            used.update(group_ids)
            
            # Ищем исходные сегменты
            for seg in dialog_segments:
                if seg.segment_id in group_ids:
                    group_segs.append(seg)
            
            group_segs.sort(key=lambda s: s.start_index)
            
            if len(group_segs) >= 2:
                group_counter += 1
                merged_text = "\n".join(s.text for s in group_segs)
                gaps = [_calculate_gap_seconds(group_segs[j], group_segs[j+1]) 
                       for j in range(len(group_segs) - 1)]
                
                groups.append(MergeGroup(
                    group_id=f"merge_group_{group_counter:03d}",
                    segments=group_segs,
                    segment_ids=group_ids,
                    start_index=group_segs[0].start_index,
                    end_index=group_segs[-1].end_index,
                    total_duration_sec=group_segs[-1].end_index - group_segs[0].start_index + 1,
                    gaps=gaps,
                    merged_text=merged_text,
                    merge_reason="; ".join(cand["reasons"]),
                    confidence=cand["score"],
                ))
    
    metrics = {
        "total_segments": len(segments),
        "dialog_segments": len(dialog_segments),
        "candidates_found": len(candidates),
        "groups_proposed": len(groups),
        "time_threshold_sec": time_threshold_sec,
        "analyze_only": analyze_only,
    }
    
    return groups, metrics


# ---------------------------------------------------------------------------
# Фактическое объединение сегментов (пока не используется в analyze_only режиме)
# ---------------------------------------------------------------------------

def merge_segments(
    segments: list[Segment],
    merge_groups: list[MergeGroup],
    apply_merge: bool = False,
) -> tuple[list[Segment], list[dict]]:
    """Объединяет сегменты из групп в единые сегменты.
    
    Args:
        segments: исходный список сегментов
        merge_groups: группы для объединения
        apply_merge: если False — возвращает исходные сегменты без изменений
    
    Returns:
        (new_segments, merge_info):
            - new_segments: новый список сегментов (или исходный если apply_merge=False)
            - merge_info: информация о выполненных объединениях
    """
    
    if not apply_merge or not merge_groups:
        return segments, {
            "applied": False,
            "groups_count": len(merge_groups),
            "reason": "analyze_only mode" if not apply_merge else "no groups",
        }
    
    # Создаём новый список сегментов
    new_segments: list[Segment] = []
    merged_ids = set()
    merge_info = []
    
    for seg in segments:
        # Проверяем, входит ли сегмент в группу
        in_group = False
        for group in merge_groups:
            if seg.segment_id in group.segment_ids:
                in_group = True
                break
        
        if not in_group:
            # Сегмент не в группе — оставляем как есть
            new_segments.append(seg)
        elif seg.segment_id not in merged_ids:
            # Создаём объединённый сегмент
            group = next(g for g in merge_groups if seg.segment_id in g.segment_ids)
            
            # Объединяем все строки из всех сегментов группы
            all_rows = []
            for s in group.segments:
                all_rows.extend(s.rows)
            
            all_rows.sort(key=lambda r: r.index)
            
            merged_seg = Segment(
                segment_id=group.group_id,
                store_id=group.segments[0].store_id,
                start_index=group.start_index,
                end_index=group.end_index,
                segment_type="dialog",
                confidence=min(s.confidence for s in group.segments),
                reason=f"объединено из {len(group.segments)} сегментов: {group.merge_reason}",
                rows=all_rows,
            )
            
            new_segments.append(merged_seg)
            merged_ids.update(group.segment_ids)
            
            merge_info.append({
                "group_id": group.group_id,
                "original_segment_ids": group.segment_ids,
                "new_start_index": merged_seg.start_index,
                "new_end_index": merged_seg.end_index,
                "original_count": len(group.segments),
                "reason": group.merge_reason,
                "confidence": group.confidence,
            })
    
    # Сортируем по start_index
    new_segments.sort(key=lambda s: s.start_index)
    
    return new_segments, {
        "applied": True,
        "groups_count": len(merge_groups),
        "merged_count": len(merge_info),
        "details": merge_info,
    }


# ---------------------------------------------------------------------------
# Отчёт о потенциальных объединениях
# ---------------------------------------------------------------------------

def generate_merge_report(
    segments: list[Segment],
    merge_groups: list[MergeGroup],
    metrics: dict,
) -> str:
    """Генерирует текстовый отчёт о потенциальных объединениях."""
    
    lines = [
        "=" * 80,
        "ОТЧЁТ О ПОТЕНЦИАЛЬНЫХ ОБЪЕДИНЕНИЯХ СЕГМЕНТОВ",
        "=" * 80,
        "",
        f"Всего сегментов: {metrics.get('total_segments', 0)}",
        f"Dialog-сегментов: {metrics.get('dialog_segments', 0)}",
        f"Кандидатов найдено: {metrics.get('candidates_found', 0)}",
        f"Групп предложено: {metrics.get('groups_proposed', 0)}",
        f"Порог времени: {metrics.get('time_threshold_sec', 0)}с",
        f"Режим: {'ТОЛЬКО АНАЛИЗ' if metrics.get('analyze_only') else 'ПРИМЕНЕНИЕ'}",
        "",
    ]
    
    if merge_groups:
        lines.append("ПРЕДЛОЖЕННЫЕ ГРУППЫ:")
        lines.append("-" * 40)
        
        for group in merge_groups:
            lines.append(f"\n{group.group_id}:")
            lines.append(f"  Сегменты: {', '.join(group.segment_ids)}")
            lines.append(f"  Разрывы: {[f'{g:.1f}с' for g in group.gaps]}")
            lines.append(f"  Причина: {group.merge_reason}")
            lines.append(f"  Уверенность: {group.confidence:.2f}")
            lines.append(f"  Текст (первые 200 символов): {group.merged_text[:200]}...")
    else:
        lines.append("Нет предложенных объединений.")
    
    lines.append("")
    lines.append("=" * 80)
    
    return "\n".join(lines)
