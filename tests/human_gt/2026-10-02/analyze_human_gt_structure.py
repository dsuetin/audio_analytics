#!/usr/bin/env python3
"""
Анализ структуры Human Ground Truth для 02.10.2026

Этот скрипт исследует:
1. Где физически хранится исходная транскрипция
2. Где и в каком виде хранится человеческая разметка
3. Как связаны исходные строки, отметки проверяющего и вручную добавленные диалоги
4. Сколько ручных оценок "правильно" и "неправильно"
5. Сколько добавленных человеком пропущенных диалогов
"""

import json
import pandas as pd
from pathlib import Path
from collections import defaultdict

REPORTS_DIR = Path("/home/developer/devbox/audio_analytics/reports")
DATE = "2026-10-02"

def main():
    print("=" * 80)
    print("СТРУКТУРА HUMAN GROUND TRUTH ДЛЯ 02.10.2026")
    print("=" * 80)
    
    # 1. Исходная транскрипция (final report)
    print("\n1. ИСХОДНАЯ ТРАНСКРИПЦИЯ (final report)")
    print("-" * 80)
    final_report_path = REPORTS_DIR / f"final_transcript_report_{DATE}.xlsx"
    df = pd.read_excel(final_report_path)
    print(f"Файл: {final_report_path}")
    print(f"Всего диалогов: {len(df)}")
    print(f"Колонки: {list(df.columns)}")
    print(f"\nМагазины ({len(df['store_id'].unique())}):")
    for store, count in df['store_id'].value_counts().items():
        print(f"  - {store}: {count} диалогов")
    
    # 2. Комментарии пользователей (comments.json)
    print("\n\n2. КОММЕНТАРИИ ПОЛЬЗОВАТЕЛЕЙ (comments.json)")
    print("-" * 80)
    comments_path = REPORTS_DIR / "comments.json"
    with open(comments_path) as f:
        comments = json.load(f)
    
    # Фильтруем комментарии для 10-02
    comments_10_02 = {k: v for k, v in comments.items() if DATE in k}
    print(f"Всего комментариев для {DATE}: {len(comments_10_02)}")
    
    # Группируем по магазину
    comments_by_store = defaultdict(list)
    for key, value in comments_10_02.items():
        parts = key.split("_")
        if len(parts) >= 3:
            store = "_".join(parts[1:-1])  # Берём всё кроме даты и client_id
            comments_by_store[store].append((key, value))
    
    print(f"\nКомментарии по магазинам:")
    for store, comment_list in sorted(comments_by_store.items()):
        print(f"  {store}: {len(comment_list)} комментариев")
    
    # 3. Ручные диалоги (manual_dialogs.json)
    print("\n\n3. РУЧНЫЕ ДИАЛОГИ (manual_dialogs.json)")
    print("-" * 80)
    manual_path = REPORTS_DIR / "manual_dialogs.json"
    with open(manual_path) as f:
        manual_dialogs = json.load(f)
    
    manual_10_02 = [d for d in manual_dialogs if d.get("date") == DATE]
    print(f"Всего ручных диалогов для {DATE}: {len(manual_10_02)}")
    
    # Группируем по магазину
    manual_by_store = defaultdict(list)
    for d in manual_10_02:
        manual_by_store[d.get("store_id")].append(d)
    
    print(f"\nРучные диалоги по магазинам:")
    for store, dlist in sorted(manual_by_store.items()):
        print(f"  {store}: {len(dlist)} диалогов")
        for d in dlist[:3]:  # Показываем первые 3
            print(f"    - {d.get('start_time')} - {d.get('end_time')}: {d.get('dialog_type')} / {d.get('result')}")
    
    # 4. Анализ формата комментариев
    print("\n\n4. ФОРМАТ КОММЕНТАРИЕВ")
    print("-" * 80)
    
    # Считаем разные типы комментариев
    comment_types = defaultdict(int)
    for key, value in comments_10_02.items():
        value_lower = value.lower().strip()
        
        if "пропущ" in value_lower:
            comment_types["пропущенный_клиент"] += 1
        elif "клиента не было" in value_lower or "клиент не было" in value_lower:
            comment_types["клиента_не_было"] += 1
        elif "1.+" in value_lower or "1+ " in value_lower:
            if "2.+" in value_lower or "2+ " in value_lower:
                comment_types["оба_правильно_1_2"] += 1
            else:
                comment_types["только_1_правильно"] += 1
        elif "1.-" in value_lower:
            if "2.-" in value_lower:
                comment_types["оба_неправильно_1_-_2_-"] += 1
            else:
                comment_types["только_1_неправильно"] += 1
        elif "1." in value_lower or "сервис" in value_lower or "покупка" in value_lower:
            comment_types["детальный_комментарий"] += 1
        else:
            comment_types["другое"] += 1
    
    print("Типы комментариев:")
    for ctype, count in sorted(comment_types.items(), key=lambda x: -x[1]):
        print(f"  {ctype}: {count}")
    
    # Примеры каждого типа
    print("\nПримеры комментариев:")
    examples_shown = 0
    for key, value in comments_10_02.items():
        if examples_shown >= 15:
            break
        print(f"  {key}: {value}")
        examples_shown += 1
    
    # 5. Сопоставление комментариев с диалогами
    print("\n\n5. СОПОСТАВЛЕНИЕ КОММЕНТАРИЕВ С ДИАЛОГАМИ")
    print("-" * 80)
    
    # Создаём карту диалогов из final report
    dialog_map = {}
    for _, row in df.iterrows():
        key = f"{DATE}_{row['store_id']}_client_{row['client_id'].replace('client_', '')}"
        dialog_map[key] = row
    
    # Сопоставляем комментарии
    matched = 0
    unmatched = 0
    manual_comments = 0
    
    for key, value in comments_10_02.items():
        if "Manual" in key:
            manual_comments += 1
        elif key in dialog_map:
            matched += 1
        else:
            unmatched += 1
    
    print(f"Комментарии сопоставлены с final report: {matched}")
    print(f"Комментарии НЕ сопоставлены: {unmatched}")
    print(f"Комментарии для Manual диалогов: {manual_comments}")
    
    # 6. Несопоставленные комментарии
    if unmatched > 0:
        print("\nНесопоставленные комментарии:")
        for key, value in comments_10_02.items():
            if "Manual" not in key and key not in dialog_map:
                print(f"  {key}: {value}")
    
    # 7. Итоговая сводка
    print("\n\n6. ИТОГОВАЯ СВОДКА")
    print("-" * 80)
    print(f"Исходная транскрипция: {len(df)} диалогов в final_report")
    print(f"Комментарии пользователей: {len(comments_10_02)}")
    print(f"  - Правильно (оба признака): {comment_types.get('оба_правильно_1_2', 0)}")
    print(f"  - Неправильно (оба признака): {comment_types.get('оба_неправильно_1_-_2_-', 0)}")
    print(f"  - Клиента не было: {comment_types.get('клиента_не_было', 0)}")
    print(f"  - Пропущенный клиент: {comment_types.get('пропущенный_клиент', 0)}")
    print(f"  - Детальные комментарии: {comment_types.get('детальный_комментарий', 0)}")
    print(f"Ручные диалоги: {len(manual_10_02)}")
    print(f"Соотношение: {len(comments_10_02)} комментариев / {len(df)} диалогов = {len(comments_10_02)/len(df)*100:.1f}%")
    
    # 8. Примеры ошибок классификации
    print("\n\n7. ПРИМЕРЫ ОШИБОК КЛАССИФИКАЦИИ")
    print("-" * 80)
    
    for key, value in comments_10_02.items():
        if "1.-" in value or "клиента не было" in value.lower():
            if key in dialog_map:
                row = dialog_map[key]
                print(f"\n{key}")
                print(f"  Комментарий: {value}")
                print(f"  Auto dialog_type: {row['dialog_type']}")
                print(f"  Auto is_sale: {row['is_sale']}")
                print(f"  Time: {row['dialog_start_at']} - {row['dialog_end_at']}")
                print(f"  Text (первые 100 символов): {row['recognition_text'][:100]}...")

if __name__ == "__main__":
    main()
