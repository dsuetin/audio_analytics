"""LLM-based segment merging.

Цель: восстановить целостные диалоги из первичных сегментов с помощью LLM-анализа
текстов соседних сегментов.

Принципы:
1. Использовать LLM для принятия решений об объединении (не эвристики)
2. Анализировать тексты соседних сегментов
3. Определять, являются ли сегменты частями одного непрерывного разговора
4. Сохранять исходные сегменты и их классификации
5. Обрабатывать ошибки и некорректные ответы модели

Отличия от segment_merge.py (эвристики):
- Решения принимает LLM на основе семантического анализа текстов
- Не опирается на временные пороги как основной критерий
- Учитывает контекст, продолжение, вопрос-ответ, смену тем
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Sequence

from .llm import OllamaClient, OllamaError, extract_json
from .segmentation import Segment, Row


# ---------------------------------------------------------------------------
# Промпт для определения продолжения разговора
# ---------------------------------------------------------------------------

SEGMENT_MERGE_SYSTEM_PROMPT = """
Ты — аналитик аудиопотока магазина. Твоя задача — определить, относятся ли
два последовательных фрагмента транскрипции к ОДНОМУ непрерывному разговору
с одним клиентом или это РАЗНЫЕ взаимодействия.

Ты получаешь два последовательных сегмента (по порядку времени) и должна
решить: объединить их в один диалог или оставить раздельно.

КРИТИЧЕСКИ ВАЖНЫЕ ПРАВИЛА:

1. СМЕНА ТЕМЫ ВНУТРИ ОДНОГО РАЗГОВОРА НЕ ОЗНАЧАЕТ НОВОГО КЛИЕНТА
   Один клиент может обсуждать много разных тем в течение одного визита:
   - технические детали → цена → покупка → гарантия → бонусы
   - товары → аксессуары → оплата → чек
   - вопрос → ответ → уточнение → ещё вопросов
   
   Если это ЛОГИЧЕСКИЕ этапы ОДНОЙ сделки — это ОДИН разговор.

2. ПРОДОЛЖЕНИЕ РАЗГОВОРА (объединить):
   - вопрос в первом сегменте → ответ во втором
   - обсуждение товара → обсуждение цены того же товара
   - выбор товара → оформление покупки → выдача чека
   - покупка → предложение бонусной программы
   - просьба оценить обслуживание после покупки
   - местоимения/ссылки на предыдущее: "этот", "такой", "он", "давайте", "хорошо"
   - естественное продолжение мысли
   - просьба проверить/завести/тестировать товар (это занимает время!)
   
    ВАЖНО: паузы 2-3 минуты МОГУТ быть внутри одного разговора, если:
    - клиент заводит машину/проверяет аккумулятор
    - сотрудник идёт за товаром/документами
    - клиент звонит кому-то для консультации
    - тестирование товара на практике
    
    ОСОБЫЙ СЛУЧАЙ: ПРИКУРИВАНИЕ АККУМУЛЯТОРА:
    - Если в первом сегменте есть просьба прикурить/бустер/прикурить машину
    - Клиент может отсутствовать ДО 30 минут (заводит машину, проверяет)
    - Следующий сегмент нужно проверять на ПРИВЕТСТВИЕ:
      * Если есть "здравствуйте", "добрый день", "добрый вечер" — НОВЫЙ КЛИЕНТ
      * Если НЕТ приветствия — это ПРОДОЛЖЕНИЕ того же клиента (после прикуривания)
    - Пример: "прикурить бустра нету" → (22 мин) → "мерса спокойно а поболел" (без приветствия) = ОДИН КЛИЕНТ

3. НОВЫЙ КЛИЕНТ (не объединять):
   - завершение (оплата/прощание/чек) + новое приветствие + новый вопрос
   - явно другой человек начинает говорить
   - между сегментами был разговор сотрудников или другого клиента
   - новый товар/модель/автомобиль, который НЕ обсуждался ранее
   - пауза 5+ минут + новое обращение (без связи с предыдущим)
   - РАЗНЫЕ автомобили/модели в разных сегментах (например, "камаз" vs "нева")
   - РАЗНЫЕ типы аккумуляторов без явной связи

4. Сомнительные случаи:
   - Если не уверен — лучше НЕ объединять (ложное объединение хуже)
   - Короткие сегменты с gap=0 чаще всего — один разговор
   - Два приветствия подряд с большим разрывом — разные клиенты
   - Если во втором сегменте есть просьба "заведите", "проверю", "тест" — это продолжение!
   - Если обсуждаются разные автомобили или аккумуляторы — это разные клиенты!

ФОРМАТ ОТВЕТА:
Только JSON-объект (без markdown, без лишних слов):
{
  "merge": true/false,
  "confidence": 0.0-1.0,
  "reason": "короткое объяснение на русском (<=200 символов)"
}
"""


def build_segment_merge_prompt(
    seg1: Segment,
    seg2: Segment,
    gap_seconds: float,
) -> str:
    """Формирует user prompt для запроса к LLM."""
    
    # Форматируем тексты сегментов
    def format_segment(seg: Segment, label: str) -> str:
        lines = [f"[{label}]"]
        if seg.start_at:
            lines.append(f"  Время: {seg.start_at:%H:%M:%S}")
        if seg.duration_sec > 0:
            lines.append(f"  Длительность: {seg.duration_sec:.1f} сек")
        lines.append(f"  Тип: {seg.segment_type}")
        lines.append(f"  Реплик: {seg.row_count}")
        lines.append("  Текст:")
        for row in seg.rows:
            text = " ".join((row.text or "").split())
            if text:
                time_str = row.created_at.strftime("%H:%M:%S") if row.created_at else "???:???:???"
                lines.append(f"    {time_str} | {text}")
        return "\n".join(lines)
    
    seg1_text = format_segment(seg1, "Сегмент 1")
    seg2_text = format_segment(seg2, "Сегмент 2")
    
    gap_info = f"Разрыв между сегментами: {gap_seconds:.1f} сек"
    
    return (
        f"АНАЛИЗИРУЙ ДВА СЕГМЕНТА:\n\n"
        f"{seg1_text}\n\n"
        f"{seg2_text}\n\n"
        f"{gap_info}\n\n"
        "Определи, относятся ли эти сегменты к ОДНОМУ непрерывному разговору "
        "с одним клиентом или это РАЗНЫЕ взаимодействия.\n\n"
        "Ответь JSON-объектом с полями merge, confidence, reason."
    )


@dataclass
class MergeDecision:
    """Решение LLM об объединении двух сегментов."""
    
    seg1_id: str
    seg2_id: str
    merge: bool
    confidence: float
    reason: str
    gap_seconds: float


@dataclass
class MergeGroup:
    """Группа сегментов, предлагаемых к объединению."""
    
    group_id: str
    segments: list[Segment]
    segment_ids: list[str]
    start_index: int
    end_index: int
    total_duration_sec: float
    gaps: list[float]
    merged_text: str
    merge_reasons: list[str] = field(default_factory=list)
    avg_confidence: float = 0.0
    
    @property
    def row_count(self) -> int:
        return self.end_index - self.start_index + 1
    
    @property
    def segment_count(self) -> int:
        return len(self.segments)


def _calculate_gap_seconds(seg1: Segment, seg2: Segment) -> float:
    """Вычисляет разрыв между двумя сегментами в секундах."""
    if seg1.end_at is not None and seg2.start_at is not None:
        return float((seg2.start_at - seg1.end_at).total_seconds())
    
    # Fallback: используем разрыв по строкам
    row_gap = seg2.start_index - seg1.end_index - 1
    if row_gap < 0:
        return 0.0
    return row_gap * 2.5


def query_llm_for_merge(
    seg1: Segment,
    seg2: Segment,
    llm: OllamaClient | None = None,
) -> MergeDecision | None:
    """Запрашивает у LLM решение об объединении двух сегментов.
    
    Args:
        seg1: первый сегмент (ранее по времени)
        seg2: второй сегмент (позже по времени)
        llm: клиент LLM (если None — используется OllamaClient по умолчанию)
    
    Returns:
        MergeDecision или None если ошибка
    """
    if llm is None:
        llm = OllamaClient()
    
    gap = _calculate_gap_seconds(seg1, seg2)
    user_prompt = build_segment_merge_prompt(seg1, seg2, gap)
    
    try:
        # Используем production budget
        num_ctx = int(os.getenv("OFFLINE_NUM_CTX", "131072"))
        num_predict = int(os.getenv("OFFLINE_NUM_PREDICT", "128000"))
        
        resp = llm.chat_json(
            SEGMENT_MERGE_SYSTEM_PROMPT,
            user_prompt,
            num_ctx=num_ctx,
            num_predict=num_predict,
            temperature=0.0,
            retries=2,
        )
        
        content = resp.get("content", "") if isinstance(resp, dict) else ""
        obj = extract_json(content)
        
        if not obj:
            return None
        
        merge = obj.get("merge", False)
        confidence = float(obj.get("confidence", 0.0))
        reason = obj.get("reason", "")
        
        if not isinstance(merge, bool):
            merge = str(merge).lower() in ("true", "1", "yes")
        
        return MergeDecision(
            seg1_id=seg1.segment_id,
            seg2_id=seg2.segment_id,
            merge=merge,
            confidence=confidence,
            reason=reason,
            gap_seconds=gap,
        )
        
    except (OllamaError, json.JSONDecodeError, TypeError, ValueError) as e:
        print(f"[segment_merge_llm] Ошибка запроса к LLM: {e}")
        return None


def _has_boosting_keywords(text: str) -> bool:
    """Проверяет, есть ли в тексте признаки прикуривания аккумулятора.
    
    Args:
        text: текст сегмента
        
    Returns:
        True если есть слова, связанные с прикуриванием/бустером
    """
    boosting_words = [
        "прикурить", "прикуривание", "бустер", "бустра", "прикурить машину",
        "прикурить аккумулятор", "прикурить авто", "прикурить автомобиль"
    ]
    text_lower = text.lower()
    return any(word in text_lower for word in boosting_words)


def find_merge_candidates_llm(
    segments: list[Segment],
    llm: OllamaClient | None = None,
    time_threshold_sec: float = 10.0,
    min_confidence: float = 0.4,
) -> tuple[list[MergeGroup], dict]:
    """Находит группы сегментов для объединения с помощью LLM.
    
    Args:
        segments: список сегментов после первичной сегментации
        llm: клиент LLM (если None — используется OllamaClient по умолчанию)
        time_threshold_sec: максимальный разрыв для рассмотрения кандидатов
        min_confidence: минимальная уверенность для предложения объединения
    
    Returns:
        (merge_groups, metrics)
    """
    if not segments:
        return [], {"candidates_found": 0, "groups_proposed": 0, "llm_calls": 0}
    
    # Фильтруем только dialog-сегменты и сортируем
    dialog_segments = [s for s in segments if s.segment_type == "dialog"]
    dialog_segments.sort(key=lambda s: (s.store_id, s.start_index))
    
    if len(dialog_segments) < 2:
        return [], {"candidates_found": 0, "groups_proposed": 0, "llm_calls": 0}
    
    # Группируем по магазинам
    decisions: list[MergeDecision] = []
    llm_calls = 0
    candidates_found = 0
    
    current_store = None
    store_segments: list[Segment] = []
    
    for seg in dialog_segments:
        if seg.store_id != current_store:
            # Обрабатываем предыдущий магазин
            if store_segments:
                for i in range(len(store_segments) - 1):
                    seg1 = store_segments[i]
                    seg2 = store_segments[i + 1]
                    
                    gap = _calculate_gap_seconds(seg1, seg2)
                    
                    # Определяем эффективный порог:
                    # Если в первом сегменте есть признаки прикуривания — увеличиваем до 30 мин
                    effective_threshold = time_threshold_sec
                    if _has_boosting_keywords(seg1.text):
                        effective_threshold = max(effective_threshold, 1800.0)  # 30 минут для прикуривания
                    
                    # Проверяем порог времени
                    if gap > effective_threshold:
                        continue
                    
                    candidates_found += 1
                    llm_calls += 1
                    
                    decision = query_llm_for_merge(seg1, seg2, llm)
                    if decision:
                        decisions.append(decision)
                
            # Начинаем новый магазин
            current_store = seg.store_id
            store_segments = [seg]
        else:
            store_segments.append(seg)
    
    # Обрабатываем последний магазин
    if store_segments:
        for i in range(len(store_segments) - 1):
            seg1 = store_segments[i]
            seg2 = store_segments[i + 1]
            
            gap = _calculate_gap_seconds(seg1, seg2)
            
            # Определяем эффективный порог для прикуривания
            effective_threshold = time_threshold_sec
            if _has_boosting_keywords(seg1.text):
                effective_threshold = max(effective_threshold, 1800.0)  # 30 минут для прикуривания
            
            if gap > effective_threshold:
                continue
            
            candidates_found += 1
            llm_calls += 1
            
            decision = query_llm_for_merge(seg1, seg2, llm)
            if decision:
                decisions.append(decision)
    
    # Строим группы из решений
    groups = _build_merge_groups_from_decisions(
        decisions, dialog_segments, min_confidence
    )
    
    metrics = {
        "total_segments": len(segments),
        "dialog_segments": len(dialog_segments),
        "candidates_found": candidates_found,
        "groups_proposed": len(groups),
        "llm_calls": llm_calls,
        "time_threshold_sec": time_threshold_sec,
        "min_confidence": min_confidence,
    }
    
    return groups, metrics


def _build_merge_groups_from_decisions(
    decisions: list[MergeDecision],
    dialog_segments: list[Segment],
    min_confidence: float,
) -> list[MergeGroup]:
    """Строит группы объединений из решений LLM."""
    
    if not decisions:
        return []
    
    # Карта сегментов по ID
    seg_map = {s.segment_id: s for s in dialog_segments}
    
    # Фильтруем решения с достаточной уверенностью
    valid_decisions = [
        d for d in decisions
        if d.merge and d.confidence >= min_confidence
    ]
    
    if not valid_decisions:
        return []
    
    # Группируем в цепи: если A→B и B→C, то объединяем A, B, C
    groups: list[MergeGroup] = []
    group_counter = 0
    
    # Сортируем решения по seg1_id для последовательной обработки
    valid_decisions.sort(key=lambda d: seg_map.get(d.seg1_id, type('obj', (object,), {'start_index': float('inf')})()).start_index)
    
    for decision in valid_decisions:
        seg1 = seg_map.get(decision.seg1_id)
        seg2 = seg_map.get(decision.seg2_id)
        
        if not seg1 or not seg2:
            continue
        
        # Ищем существующую группу, куда можно добавить
        existing_group = None
        for group in groups:
            if decision.seg2_id in group.segment_ids:
                # seg2 уже в группе, добавляем после неё
                existing_group = group
                break
            elif decision.seg1_id in group.segment_ids:
                # seg1 уже в группе, проверяем если seg2 идёт после
                last_seg_id = group.segment_ids[-1]
                if decision.seg1_id == last_seg_id:
                    existing_group = group
                    break
        
        if existing_group:
            # Добавляем к существующей группе
            if decision.seg2_id not in existing_group.segment_ids:
                existing_group.segment_ids.append(decision.seg2_id)
                existing_group.segments.append(seg2)
                existing_group.end_index = seg2.end_index
                existing_group.merged_text = "\n".join(s.text for s in existing_group.segments if s.text)
                existing_group.merge_reasons.append(decision.reason)
                existing_group.segments.sort(key=lambda s: s.start_index)
                existing_group.segment_ids = [s.segment_id for s in existing_group.segments]
                existing_group.gaps = [_calculate_gap_seconds(existing_group.segments[j], existing_group.segments[j+1])
                                       for j in range(len(existing_group.segments) - 1)]
        else:
            # Начинаем новую группу
            group_counter += 1
            group_segs = [seg1, seg2]
            group_ids = [decision.seg1_id, decision.seg2_id]
            merged_text = "\n".join(s.text for s in group_segs if s.text)
            gaps = [_calculate_gap_seconds(group_segs[j], group_segs[j+1])
                   for j in range(len(group_segs) - 1)]
            
            avg_conf = decision.confidence
            
            groups.append(MergeGroup(
                group_id=f"merge_group_{group_counter:03d}",
                segments=group_segs,
                segment_ids=group_ids,
                start_index=group_segs[0].start_index,
                end_index=group_segs[-1].end_index,
                total_duration_sec=_calculate_gap_seconds(group_segs[0], group_segs[-1]),
                gaps=gaps,
                merged_text=merged_text,
                merge_reasons=[decision.reason],
                avg_confidence=avg_conf,
            ))
    
    return groups


def merge_segments(
    segments: list[Segment],
    merge_groups: list[MergeGroup],
    apply_merge: bool = False,
) -> tuple[list[Segment], dict]:
    """Объединяет сегменты из групп в единые сегменты.
    
    Args:
        segments: исходный список сегментов
        merge_groups: группы для объединения
        apply_merge: если False — возвращает исходные сегменты
    
    Returns:
        (new_segments, merge_info)
    """
    
    if not apply_merge or not merge_groups:
        return segments, {
            "applied": False,
            "groups_count": len(merge_groups),
            "reason": "analyze_only mode" if not apply_merge else "no groups",
        }
    
    new_segments: list[Segment] = []
    merged_ids = set()
    merge_info = []
    
    for seg in segments:
        in_group = False
        target_group = None
        for group in merge_groups:
            if seg.segment_id in group.segment_ids:
                in_group = True
                target_group = group
                break
        
        if not in_group:
            new_segments.append(seg)
        elif seg.segment_id not in merged_ids:
            # Создаём объединённый сегмент
            all_rows = []
            for s in target_group.segments:
                all_rows.extend(s.rows)
            
            all_rows.sort(key=lambda r: r.index)
            
            store_id = target_group.segments[0].store_id
            
            merged_seg = Segment(
                segment_id=target_group.group_id,
                store_id=store_id,
                start_index=target_group.start_index,
                end_index=target_group.end_index,
                segment_type="dialog",
                confidence=min(s.confidence for s in target_group.segments),
                reason=f"объединено из {len(target_group.segments)} сегментов (LLM)",
                rows=all_rows,
            )
            
            # Сохраняем связь с исходными сегментами
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
                "reason": "; ".join(target_group.merge_reasons),
                "confidence": target_group.avg_confidence,
                "gaps_sec": target_group.gaps,
            })
    
    new_segments.sort(key=lambda s: s.start_index)
    
    return new_segments, {
        "applied": True,
        "groups_count": len(merge_groups),
        "merged_count": len(merge_info),
        "details": merge_info,
    }


def generate_merge_report(
    segments: list[Segment],
    merge_groups: list[MergeGroup],
    metrics: dict,
) -> str:
    """Генерирует текстовый отчёт о потенциальных объединениях."""
    
    lines = [
        "=" * 80,
        "ОТЧЁТ О ПОТЕНЦИАЛЬНЫХ ОБЪЕДИНЕНИЯХ СЕГМЕНТОВ (LLM)",
        "=" * 80,
        "",
        f"Всего сегментов: {metrics.get('total_segments', 0)}",
        f"Dialog-сегментов: {metrics.get('dialog_segments', 0)}",
        f"Кандидатов проверено: {metrics.get('candidates_found', 0)}",
        f"LLM-вызовов: {metrics.get('llm_calls', 0)}",
        f"Групп предложено: {metrics.get('groups_proposed', 0)}",
        f"Порог времени: {metrics.get('time_threshold_sec', 0)}с",
        f"Мин. уверенность: {metrics.get('min_confidence', 0)}",
        "",
    ]
    
    if merge_groups:
        lines.append("ПРЕДЛОЖЕННЫЕ ГРУППЫ:")
        lines.append("-" * 40)
        
        for group in merge_groups:
            lines.append(f"\n{group.group_id}:")
            lines.append(f"  Сегменты: {', '.join(group.segment_ids)}")
            lines.append(f"  Разрывы: {[f'{g:.1f}с' for g in group.gaps]}")
            lines.append(f"  Причины: {'; '.join(group.merge_reasons)}")
            lines.append(f"  Уверенность: {group.avg_confidence:.2f}")
            preview = group.merged_text[:200] if group.merged_text else "(нет текста)"
            lines.append(f"  Текст (первые 200 символов): {preview}...")
    else:
        lines.append("Нет предложенных объединений.")
    
    lines.append("")
    lines.append("=" * 80)
    
    return "\n".join(lines)
