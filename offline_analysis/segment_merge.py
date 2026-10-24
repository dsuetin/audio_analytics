"""Secondary segmentation: merging related speech segments.

Цель: восстановить целостные диалоги из первичных сегментов, которые относятся
к одному разговору, но были разделены на этапе первичной сегментации.

Принципы:
1. Рассматривать сегменты в хронологическом порядке
2. Анализировать соседние сегменты одного аудиопотока/продавца/магазина
3. Использовать разрыв < 10 секунд как критерий поиска кандидатов
4. НЕ считать разные классы взаимодействия основанием для разделения
5. Использовать семантические признаки для принятия решения об объединении
6. Сохранять исходные сегменты и их классификации
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
    start_index: int  # первая строка первого сегмента
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
# Семантический анализ для принятия решений об объединении
# ---------------------------------------------------------------------------

# Паттерны для обнаружения признаков продолжения разговора
_CONTINUATION_PATTERNS = [
    r"(?:^|\s)(далее|дальше|потом|затем|ещё|еще|также|тоже|а( |:|,)|но|или)\b",
    r"(?:^|\s)(этот|такой|тот|тут|там|вот|он|она|оно|они)\b",
    r"(?:^|\s)(да(вайте)?|хорошо|конечно|угу|ага|понятно|ясно)\b",
]

# Паттерны для обнаружения завершения разговора (сигналы конца взаимодействия)
_COMPLETION_PATTERNS = [
    r"(?:^|\s)(до\s+свидания|до\s+встреч|всего\s+хорошего|успех\w*|будем\s+рады\s+видеть|заходите\s+ещё)\b",
    r"(?:^|\s)(чек|оплат\w*|терминал|гарантийный\s+талон|бонус\w*|карта)\b",
]

# Паттерны для нового приветствия (сигналы нового клиента)
_GREETING_PATTERNS = [
    r"(?:^|\s)(здравств\w*|добрый\s+(?:день|вечер|утро)|добрый\w*|дарово|привет)\b",
]

# Паттерны для вопросов (сигналы начала/продолжения диалога)
_QUESTION_PATTERNS = [
    r"(?:^|\s)(сколько|какой|какая|какие|есть|цена|стоит|доступен|можно|как)\b",
]


def _text_matches_pattern(text: str, patterns: list[str]) -> bool:
    """Проверка, содержит ли текст хотя бы один из паттернов."""
    if not text:
        return False
    text_lower = text.lower()
    for pattern in patterns:
        if re.search(pattern, text_lower):
            return True
    return False


def _analyze_continuation(seg1: Segment, seg2: Segment) -> dict:
    """Анализирует признаки продолжения разговора между двумя сегментами."""
    
    text1 = seg1.text.lower() if seg1.text else ""
    text2 = seg2.text.lower() if seg2.text else ""
    
    # Проверка на продолжение во втором сегменте
    has_continuation = _text_matches_pattern(text2, _CONTINUATION_PATTERNS)
    
    # Проверка на вопрос-ответ паттерн
    has_question_in_first = _text_matches_pattern(text1, _QUESTION_PATTERNS) or '?' in text1
    has_answer_in_second = _text_matches_pattern(text2, _CONTINUATION_PATTERNS)
    has_qa_pattern = has_question_in_first and has_answer_in_second
    
    # Проверка на общие темы (товар, модель, цена)
    common_topics = []
    topic_words = ['аккумулятор', 'акб', 'батарея', 'цена', 'стоит', 'сколько', 
                   'модель', 'автомобиль', 'машина', 'пусковой', 'заряд', 'газ']
    for word in topic_words:
        if word in text1 and word in text2:
            common_topics.append(word)
    
    # Проверка на лексическую связность (продолжение мысли)
    has_pronoun_continuation = bool(re.search(
        r"(?:^|\s)(он|она|оно|они|этот|эта|это|эти|такой|такая|такие)\b",
        text2
    ))
    
    return {
        "has_continuation": has_continuation,
        "has_qa_pattern": has_qa_pattern,
        "has_pronoun_continuation": has_pronoun_continuation,
        "common_topics": common_topics,
        "common_topics_count": len(common_topics),
    }


def _analyze_completion(seg: Segment) -> dict:
    """Анализирует признаки завершения разговора в сегменте."""
    text = seg.text.lower() if seg.text else ""
    
    has_completion = _text_matches_pattern(text, _COMPLETION_PATTERNS)
    has_greeting = _text_matches_pattern(text, _GREETING_PATTERNS)
    
    return {
        "has_completion": has_completion,
        "has_greeting": has_greeting,
    }


def _calculate_gap_seconds(seg1: Segment, seg2: Segment) -> float:
    """Вычисляет разрыв между двумя сегментами в секундах."""
    if seg1.end_at is not None and seg2.start_at is not None:
        return float((seg2.start_at - seg1.end_at).total_seconds())
    
    # Fallback: используем разрыв по строкам как эвристику
    row_gap = seg2.start_index - seg1.end_index - 1
    if row_gap < 0:
        return 0.0
    # Эвристика: 1 строка ≈ 2.5 секунды
    return row_gap * 2.5


def _should_not_merge(seg1: Segment, seg2: Segment) -> tuple[bool, str]:
    """Проверка, НЕ следует ли объединять сегменты (признаки нового клиента)."""
    
    # Если есть завершение в первом сегменте и приветствие во втором — это разные клиенты
    completion1 = _analyze_completion(seg1)
    completion2 = _analyze_completion(seg2)
    
    if completion1["has_completion"] and completion2["has_greeting"]:
        return True, "завершение_первого_и_приветствие_второго"
    
    # Если оба сегмента имеют приветствия в начале — вероятно, разные клиенты
    # Но не всегда: проверим контекст (если сегменты очень близки по времени — возможно, один клиент)
    if completion1["has_greeting"] and completion2["has_greeting"]:
        gap = _calculate_gap_seconds(seg1, seg2)
        # Если разрыв > 5 секунд и оба имеют приветствия — скорее всего разные клиенты
        if gap > 5:
            return True, "два_приветствия_с_большим_разрывом"
    
    return False, ""


def _calculate_merge_score(seg1: Segment, seg2: Segment) -> tuple[float, list[str]]:
    """Рассчитывает вероятность объединения двух сегментов.
    
    Returns:
        (score, reasons): 
            - score: вероятность от 0.0 до 1.0
            - reasons: список причин для принятия решения
    """
    score = 0.0
    reasons = []
    
    # 1. Временной разрыв (главный фактор)
    gap = _calculate_gap_seconds(seg1, seg2)
    
    if gap == 0:
        # Сегменты идут подряд без разрыва — сильный сигнал продолжения
        score += 0.4
        reasons.append("сегменты_идут_подряд")
    elif gap < 5:
        score += 0.3
        reasons.append(f"малый_разрыв_{gap:.1f}с")
    elif gap < 10:
        score += 0.2
        reasons.append(f"разрыв_{gap:.1f}с")
    elif gap < 15:
        score += 0.1
        reasons.append(f"разрыв_{gap:.1f}с")
    else:
        # Большой разрыв — не кандидат на объединение
        return 0.0, ["разрыв_слишком_большой"]
    
    # 2. Семантические признаки продолжения
    continuation = _analyze_continuation(seg1, seg2)
    
    if continuation["has_continuation"]:
        score += 0.15
        reasons.append("признаки_продолжения")
    
    if continuation["has_qa_pattern"]:
        score += 0.15
        reasons.append("вопрос_ответ")
    
    if continuation["has_pronoun_continuation"]:
        score += 0.1
        reasons.append("местоимение_продолжение")
    
    if continuation["common_topics_count"] >= 2:
        score += 0.15
        reasons.append(f"общие_темы_{','.join(continuation['common_topics'][:3])}")
    elif continuation["common_topics_count"] == 1:
        score += 0.05
        reasons.append(f"общая_тема_{continuation['common_topics'][0]}")
    
    # 3. Проверка на признаки нового клиента (снижение scores)
    should_not, not_reason = _should_not_merge(seg1, seg2)
    if should_not:
        score = max(0, score - 0.5)  # Сильное снижение
        reasons.append(f"НЕ_объединять_{not_reason}")
    
    return min(1.0, score), reasons


# ---------------------------------------------------------------------------
# Поиск кандидатов на объединение
# ---------------------------------------------------------------------------

def find_merge_candidates(
    segments: list[Segment],
    time_threshold_sec: float = 10.0,
    min_confidence: float = 0.5,
    analyze_only: bool = True,
) -> tuple[list[MergeGroup], dict]:
    """Находит группы сегментов, которые могут относиться к одному разговору.
    
    Args:
        segments: список сегментов после первичной сегментации
        time_threshold_sec: максимальный разрыв в секундах для рассмотрения
        min_confidence: минимальная уверенность для предложения объединения
        analyze_only: если True — только анализ, без фактического объединения
    
    Returns:
        (merge_groups, metrics):
            - merge_groups: группы сегментов, предлагаемые к объединению
            - metrics: статистика анализа
    """
    
    if not segments:
        return [], {"candidates_found": 0, "groups_proposed": 0}
    
    # Фильтруем только dialog-сегменты и сортируем по времени
    dialog_segments = [s for s in segments if s.segment_type == "dialog"]
    dialog_segments.sort(key=lambda s: (s.store_id, s.start_index))
    
    if len(dialog_segments) < 2:
        return [], {"candidates_found": 0, "groups_proposed": 0, "dialog_count": len(dialog_segments)}
    
    candidates = []
    
    # Проходим по соседним dialog-сегментам ОДНОГО МАГАЗИНА
    current_store = None
    store_segments = []
    
    for seg in dialog_segments:
        if seg.store_id != current_store:
            # Обработка предыдущего магазина
            if store_segments:
                candidates.extend(_find_candidates_in_store(
                    store_segments, time_threshold_sec, min_confidence
                ))
            # Начало нового магазина
            current_store = seg.store_id
            store_segments = [seg]
        else:
            store_segments.append(seg)
    
    # Обработка последнего магазина
    if store_segments:
        candidates.extend(_find_candidates_in_store(
            store_segments, time_threshold_sec, min_confidence
        ))
    
    # Группируем кандидатов в цепи (seg1-seg2-seg3...)
    groups: list[MergeGroup] = []
    if candidates:
        groups = _build_merge_groups(candidates, dialog_segments)
    
    metrics = {
        "total_segments": len(segments),
        "dialog_segments": len(dialog_segments),
        "candidates_found": len(candidates),
        "groups_proposed": len(groups),
        "time_threshold_sec": time_threshold_sec,
        "min_confidence": min_confidence,
        "analyze_only": analyze_only,
    }
    
    return groups, metrics


def _find_candidates_in_store(
    segments: list[Segment],
    time_threshold_sec: float,
    min_confidence: float,
) -> list[dict]:
    """Находит кандидатов на объединение внутри одного магазина."""
    
    candidates = []
    
    for i in range(len(segments) - 1):
        seg1 = segments[i]
        seg2 = segments[i + 1]
        
        # Вычисляем разрыв
        gap = _calculate_gap_seconds(seg1, seg2)
        
        # Если разрыв превышает порог — пропускаем
        if gap > time_threshold_sec:
            continue
        
        # Рассчитываем score объединения
        score, reasons = _calculate_merge_score(seg1, seg2)
        
        # Добавляем как кандидата только если есть достаточная уверенность
        if score >= min_confidence:
            candidates.append({
                "seg1_id": seg1.segment_id,
                "seg2_id": seg2.segment_id,
                "seg1_index": seg1.start_index,
                "seg2_index": seg2.start_index,
                "gap_sec": gap,
                "score": score,
                "reasons": reasons,
            })
    
    return candidates


def _build_merge_groups(
    candidates: list[dict],
    dialog_segments: list[Segment],
) -> list[MergeGroup]:
    """Строит группы объединений из кандидатов."""
    
    if not candidates:
        return []
    
    # Создаём карту сегментов по ID
    seg_map = {s.segment_id: s for s in dialog_segments}
    
    # Группируем в цепи
    groups = []
    used = set()
    group_counter = 0
    
    # Сортируем кандидатов по score (сперва наиболее уверенные)
    sorted_candidates = sorted(candidates, key=lambda x: -x["score"])
    
    for cand in sorted_candidates:
        if cand["seg1_id"] in used or cand["seg2_id"] in used:
            continue
        
        # Начинаем новую группу с этого кандидата
        group_segs = []
        group_ids = [cand["seg1_id"], cand["seg2_id"]]
        used.update(group_ids)
        
        # Ищем исходные сегменты и сортируем по индексу
        for seg_id in group_ids:
            if seg_id in seg_map:
                group_segs.append(seg_map[seg_id])
        
        group_segs.sort(key=lambda s: s.start_index)
        
        if len(group_segs) >= 2:
            group_counter += 1
            merged_text = "\n".join(s.text for s in group_segs if s.text)
            gaps = [_calculate_gap_seconds(group_segs[j], group_segs[j+1]) 
                   for j in range(len(group_segs) - 1)]
            
            # Объединяем reasons из всех кандидатов в группе
            all_reasons = []
            for c in sorted_candidates:
                if c["seg1_id"] in group_ids or c["seg2_id"] in group_ids:
                    all_reasons.extend(c["reasons"])
            
            groups.append(MergeGroup(
                group_id=f"merge_group_{group_counter:03d}",
                segments=group_segs,
                segment_ids=group_ids,
                start_index=group_segs[0].start_index,
                end_index=group_segs[-1].end_index,
                total_duration_sec=_calculate_gap_seconds(group_segs[0], group_segs[-1]),
                gaps=gaps,
                merged_text=merged_text,
                merge_reason="; ".join(set(all_reasons)),
                confidence=cand["score"],
            ))
    
    return groups


# ---------------------------------------------------------------------------
# Фактическое объединение сегментов
# ---------------------------------------------------------------------------

def merge_segments(
    segments: list[Segment],
    merge_groups: list[MergeGroup],
    apply_merge: bool = False,
) -> tuple[list[Segment], dict]:
    """Объединяет сегменты из групп в единые сегменты.
    
    Args:
        segments: исходный список сегментов
        merge_groups: группы для объединения
        apply_merge: если False — возвращает исходные сегменты без изменений
    
    Returns:
        (new_segments, merge_info):
            - new_segments: новый список сегментов (или исходный если apply_merge=False)
            - merge_info: информация о выполненных объединениях
    
    Важно:
        - Исходные сегменты НЕ изменяются
        - Создаются новые объединённые сегменты
        - Связь с исходными сегментами сохраняется через segment_ids
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
        target_group = None
        for group in merge_groups:
            if seg.segment_id in group.segment_ids:
                in_group = True
                target_group = group
                break
        
        if not in_group:
            # Сегмент не в группе — оставляем как есть
            new_segments.append(seg)
        elif seg.segment_id not in merged_ids:
            # Создаём объединённый сегмент
            # Объединяем все строки из всех сегментов группы
            all_rows = []
            for s in target_group.segments:
                all_rows.extend(s.rows)
            
            all_rows.sort(key=lambda r: r.index)
            
            # Определяем store_id и seller_id из первого сегмента
            store_id = target_group.segments[0].store_id
            first_seller = None
            for s in target_group.segments:
                if s.seller_id:
                    first_seller = s.seller_id
                    break
            
            merged_seg = Segment(
                segment_id=target_group.group_id,
                store_id=store_id,
                start_index=target_group.start_index,
                end_index=target_group.end_index,
                segment_type="dialog",
                confidence=min(s.confidence for s in target_group.segments),
                reason=f"объединено из {len(target_group.segments)} сегментов: {target_group.merge_reason}",
                rows=all_rows,
            )
            
            # Сохраняем информацию об исходных сегментах
            merged_seg._original_segment_ids = target_group.segment_ids
            merged_seg._original_segments = target_group.segments
            
            new_segments.append(merged_seg)
            merged_ids.update(target_group.segment_ids)
            
            merge_info.append({
                "group_id": target_group.group_id,
                "original_segment_ids": target_group.segment_ids,
                "new_start_index": merged_seg.start_index,
                "new_end_index": merged_seg.end_index,
                "original_count": len(target_group.segments),
                "reason": target_group.merge_reason,
                "confidence": target_group.confidence,
                "gaps_sec": target_group.gaps,
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
        f"Мин. уверенность: {metrics.get('min_confidence', 0)}",
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
            preview = group.merged_text[:200] if group.merged_text else "(нет текста)"
            lines.append(f"  Текст (первые 200 символов): {preview}...")
    else:
        lines.append("Нет предложенных объединений.")
    
    lines.append("")
    lines.append("=" * 80)
    
    return "\n".join(lines)
