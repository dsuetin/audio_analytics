#!/usr/bin/env python3
"""
Создание детального Excel-отчёта по сравнению автоматической и человеческой разметки за 02.10.2026

Генерирует 10 листов:
1. Сводка
2. По магазинам
3. Магазин × цель
4. Магазин × миссия
5. Все диалоги
6. Несовпадения
7. Ручные пропуски
8. Ложные срабатывания
9. Неоднозначные
10. Методика
"""

import json
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime
from collections import defaultdict

REPORTS_DIR = Path("/home/developer/devbox/audio_analytics/reports")
DATE = "2026-10-02"

def parse_comment(comment):
    """
    Парсит комментарий и определяет:
    - dialog_type_correct: правильно ли определена цель визита (1.)
    - is_sale_correct: правильно ли определена покупка (2.)
    - is_false_positive: это ложный диалог ("клиента не было")
    - is_missed: это пропущенный диалог
    - raw_type: указанная цель визита
    - raw_sale: указанный результат
    """
    comment_lower = comment.lower().strip()
    
    result = {
        "dialog_type_correct": None,
        "is_sale_correct": None,
        "is_false_positive": False,
        "is_missed": False,
        "raw_type": None,
        "raw_sale": None,
        "category": "unknown"
    }
    
    # Проверяем на "клиента не было"
    if "клиента не было" in comment_lower or "клиент не было" in comment_lower:
        result["is_false_positive"] = True
        result["category"] = "false_positive"
        return result
    
    # Проверяем на "пропущенный клиент"
    if "пропущ" in comment_lower:
        result["is_missed"] = True
        result["category"] = "missed"
        return result
    
    # Парсим формат "1.+ 2.+" или "1.- 2.-" или "1.покупка 2.покупка"
    
    # Извлекаем части 1. и 2.
    parts = comment_lower.split()
    part1 = None
    part2 = None
    
    for part in parts:
        if part.startswith("1."):
            part1 = part[2:].strip()
        elif part.startswith("2."):
            part2 = part[2:].strip()
    
    # Анализ part1 (dialog_type)
    if part1:
        if part1 == "+":
            result["dialog_type_correct"] = True
            result["category"] = "correct" if result["category"] == "unknown" else result["category"]
        elif part1 == "-":
            result["dialog_type_correct"] = False
            result["category"] = "incorrect"
        elif "сервис" in part1 or "сервисное" in part1:
            result["raw_type"] = "service"
            result["dialog_type_correct"] = True
            result["category"] = "correct" if result["category"] == "unknown" else result["category"]
        elif "покупка" in part1:
            result["raw_type"] = "buy"
            result["dialog_type_correct"] = True
            result["category"] = "correct" if result["category"] == "unknown" else result["category"]
        elif "помощь" in part1 or "help" in part1:
            result["raw_type"] = "help"
            result["dialog_type_correct"] = True
        elif "вакансия" in part1 or "vacancy" in part1:
            result["raw_type"] = "vacancy"
            result["dialog_type_correct"] = True
        elif "корпоратив" in part1 or "corporate" in part1:
            result["raw_type"] = "corporate"
            result["dialog_type_correct"] = True
        else:
            result["raw_type"] = part1
    
    # Анализ part2 (is_sale/result)
    if part2:
        if part2 == "+":
            result["is_sale_correct"] = True
            if result["category"] == "unknown":
                result["category"] = "correct"
        elif part2 == "-":
            result["is_sale_correct"] = False
            result["category"] = "incorrect"
        elif "покупка" in part2:
            result["raw_sale"] = "buy"
            result["is_sale_correct"] = True
            if result["category"] == "unknown":
                result["category"] = "correct"
        elif "нет покупки" in part2 or "не оказалось" in part2:
            result["raw_sale"] = "no_buy"
            result["is_sale_correct"] = True
            if result["category"] == "unknown":
                result["category"] = "correct"
        else:
            result["raw_sale"] = part2
    
    return result

def main():
    print("Загрузка данных...")
    
    # Load final report
    df_auto = pd.read_excel(REPORTS_DIR / f"final_transcript_report_{DATE}.xlsx")
    print(f"  Автоматические диалоги: {len(df_auto)}")
    
    # Load comments
    with open(REPORTS_DIR / "comments.json") as f:
        comments = json.load(f)
    comments_10_02 = {k: v for k, v in comments.items() if DATE in k}
    print(f"  Комментарии: {len(comments_10_02)}")
    
    # Load manual dialogs
    with open(REPORTS_DIR / "manual_dialogs.json") as f:
        manual_dialogs = json.load(f)
    manual_10_02 = [d for d in manual_dialogs if d.get("date") == DATE]
    print(f"  Ручные диалоги: {len(manual_10_02)}")
    
    # Load text check results
    with open(REPORTS_DIR / "manual_dialogs_text_check_2026-10-02.json") as f:
        text_check = json.load(f)
    
    # Create dialog map
    dialog_map = {}
    for _, row in df_auto.iterrows():
        client_num = row['client_id'].replace('client_', '')
        key = f"{DATE}_{row['store_id']}_client_{client_num}"
        dialog_map[key] = row
    
    # Build unified registry
    print("\nПостроение единого реестра...")
    
    all_records = []
    
    # Add auto dialogs
    for idx, row in df_auto.iterrows():
        client_num = row['client_id'].replace('client_', '')
        key = f"{DATE}_{row['store_id']}_client_{client_num}"
        
        comment = comments_10_02.get(key, "")
        parsed = parse_comment(comment) if comment else {}
        
        record = {
            "store_id": row['store_id'],
            "date": DATE,
            "start_time": row['dialog_start_at'],
            "end_time": row['dialog_end_at'],
            "auto_dialog_id": f"client_{client_num}",
            "manual_dialog_id": None,
            "comment_key": key,
            "client_id": row['client_id'],
            "recognition_text": row['recognition_text'][:500] if pd.notna(row['recognition_text']) else "",
            "auto_dialog_type": row['dialog_type'],
            "auto_is_sale": row['is_sale'],
            "human_comment": comment,
            "human_dialog_type": parsed.get("raw_type") if parsed else None,
            "human_is_sale": parsed.get("raw_sale") if parsed else None,
            "match_result": "matched" if comment else "no_human_evaluation",
            "has_text_in_db": True,
            "final_category": parsed.get("category", "no_human_evaluation") if parsed else "no_human_evaluation",
            "error_reason": None,
            "note": ""
        }
        
        if parsed:
            if parsed.get("is_false_positive"):
                record["final_category"] = "false_positive"
                record["error_reason"] = "Клиента не было"
            elif parsed.get("is_missed"):
                record["final_category"] = "missed_dialog"
            elif parsed.get("dialog_type_correct") is False or parsed.get("is_sale_correct") is False:
                record["final_category"] = "classification_error"
                record["error_reason"] = "Ошибка классификации"
            elif parsed.get("dialog_type_correct") is True and parsed.get("is_sale_correct") is True:
                record["final_category"] = "confirmed_match"
        
        all_records.append(record)
    
    # Add manual dialogs
    for md in manual_10_02:
        if md.get("store_id") == "test_store":
            continue
        
        key = f"{DATE}_{md['store_id']}_Manual"
        comment = comments_10_02.get(key, "")
        
        # Check text in DB
        has_text = False
        text_record = None
        for item in text_check.get("has_text", []):
            if (item["store_id"] == md["store_id"] and 
                item["start_time"] == md["start_time"] and 
                item["end_time"] == md["end_time"]):
                has_text = True
                text_record = item
                break
        
        for item in text_check.get("no_text", []):
            if (item["store_id"] == md["store_id"] and 
                item["start_time"] == md["start_time"] and 
                item["end_time"] == md["end_time"]):
                has_text = False
                break
        
        record = {
            "store_id": md["store_id"],
            "date": DATE,
            "start_time": md["start_time"],
            "end_time": md["end_time"],
            "auto_dialog_id": None,
            "manual_dialog_id": md["id"],
            "comment_key": key,
            "client_id": "Manual",
            "recognition_text": f"Тип: {md.get('dialog_type')}, Итог: {md.get('result')}",
            "auto_dialog_type": None,
            "auto_is_sale": None,
            "human_comment": comment,
            "human_dialog_type": md.get("dialog_type"),
            "human_is_sale": md.get("result"),
            "match_result": "manual_only",
            "has_text_in_db": has_text,
            "final_category": "missing_dialog_text_found" if has_text else "missing_dialog_no_text",
            "error_reason": "Пропущенный диалог" if has_text else "Текст не найден в БД",
            "note": f"Transcripts: {text_record['transcript_count'] if text_record else 0}" if text_record else ""
        }
        
        all_records.append(record)
    
    print(f"  Всего записей: {len(all_records)}")
    
    # Create Excel workbook
    print("\nСоздание Excel-отчёта...")
    output_file = REPORTS_DIR / "HUMAN_GT_DETAILED_ANALYSIS_2026-10-02.xlsx"
    
    with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
        # ЛИСТ 1: Сводка
        print("  Лист 1: Сводка...")
        
        summary_data = {
            "Показатель": [
                "Дата анализа",
                "Количество магазинов",
                "Количество автоматических диалогов",
                "Количество диалогов с человеческой оценкой",
                "Количество однозначно сопоставленных записей",
                "Количество подтверждённых совпадений",
                "Количество подтверждённых ошибок классификации",
                "Количество ложных срабатываний",
                "Количество вручную добавленных пропущенных диалогов",
                "Количество ручных пропусков с текстом в БД",
                "Количество ручных пропусков без текста в БД",
                "Количество неоднозначных случаев",
                "",
                "Accuracy (на проверенных бинарных оценках)",
                "Правильные классификации",
                "Неправильные классификации",
                "Всего проверено"
            ],
            "Значение": [
                DATE,
                len(df_auto['store_id'].unique()),
                len(df_auto),
                sum(1 for r in all_records if r["human_comment"]),
                sum(1 for r in all_records if r["match_result"] == "matched"),
                sum(1 for r in all_records if r["final_category"] == "confirmed_match"),
                sum(1 for r in all_records if r["final_category"] == "classification_error"),
                sum(1 for r in all_records if r["final_category"] == "false_positive"),
                len([r for r in all_records if r["client_id"] == "Manual"]),
                len([r for r in all_records if r["final_category"] == "missing_dialog_text_found"]),
                len([r for r in all_records if r["final_category"] == "missing_dialog_no_text"]),
                sum(1 for r in all_records if r["final_category"] in ["unknown", "ambiguous"]),
                "",
                f"{sum(1 for r in all_records if r['final_category'] == 'confirmed_match') / max(1, sum(1 for r in all_records if r['final_category'] in ['confirmed_match', 'classification_error'])) * 100:.1f}%",
                sum(1 for r in all_records if r["final_category"] == "confirmed_match"),
                sum(1 for r in all_records if r["final_category"] == "classification_error"),
                sum(1 for r in all_records if r["final_category"] in ["confirmed_match", "classification_error"])
            ]
        }
        
        df_summary = pd.DataFrame(summary_data)
        df_summary.to_excel(writer, sheet_name="Сводка", index=False)
        
        # ЛИСТ 2: По магазинам
        print("  Лист 2: По магазинам...")
        
        store_stats = []
        for store in sorted(df_auto['store_id'].unique()):
            store_records = [r for r in all_records if r["store_id"] == store]
            auto_records = [r for r in store_records if r["auto_dialog_id"]]
            manual_records = [r for r in store_records if r["client_id"] == "Manual"]
            
            correct = sum(1 for r in store_records if r["final_category"] == "confirmed_match")
            incorrect = sum(1 for r in store_records if r["final_category"] == "classification_error")
            false_positives = sum(1 for r in store_records if r["final_category"] == "false_positive")
            missing_text_found = sum(1 for r in store_records if r["final_category"] == "missing_dialog_text_found")
            missing_no_text = sum(1 for r in store_records if r["final_category"] == "missing_dialog_no_text")
            
            accuracy = (correct / (correct + incorrect) * 100) if (correct + incorrect) > 0 else None
            
            # Determine main problem
            if missing_text_found > incorrect:
                main_problem = "Пропущенные диалоги (текст в БД)"
                recommendation = "Проверить segmentation: gap thresholds, VAD boundaries"
            elif false_positives > incorrect:
                main_problem = "Ложные срабатывания"
                recommendation = "Улучшить детекцию внутренних разговоров"
            elif incorrect > 0:
                main_problem = "Ошибки классификации"
                recommendation = "Проверить промпт классификации"
            elif missing_no_text > 0:
                main_problem = "Отсутствие текста в БД"
                recommendation = "Проверить аудио/VAD/ASR"
            else:
                main_problem = "Значимых проблем не выявлено"
                recommendation = "-"
            
            store_stats.append({
                "Магазин": store,
                "Автоматических диалогов": len(auto_records),
                "Диалогов с оценкой": sum(1 for r in store_records if r["human_comment"]),
                "Сопоставленных": sum(1 for r in store_records if r["match_result"] == "matched"),
                "Правильных": correct,
                "Неправильных": incorrect,
                "Accuracy (%)": f"{accuracy:.1f}" if accuracy else "-",
                "Ложных срабатываний": false_positives,
                "Ручных пропущенных": len(manual_records),
                "С текстом в БД": missing_text_found,
                "Без текста в БД": missing_no_text,
                "Неоднозначных": sum(1 for r in store_records if r["final_category"] in ["unknown", "ambiguous"]),
                "Основная проблема": main_problem,
                "Рекомендация": recommendation
            })
        
        df_stores = pd.DataFrame(store_stats)
        df_stores = df_stores.sort_values("Неправильных", ascending=False)
        df_stores.to_excel(writer, sheet_name="По магазинам", index=False)
        
        # ЛИСТ 3: Магазин × цель
        print("  Лист 3: Магазин × цель...")
        
        store_type_stats = []
        for store in sorted(df_auto['store_id'].unique()):
            for dialog_type in sorted(df_auto['dialog_type'].unique()):
                if pd.isna(dialog_type):
                    continue
                
                store_type_records = [
                    r for r in all_records 
                    if r["store_id"] == store and r["auto_dialog_type"] == dialog_type
                ]
                
                correct = sum(1 for r in store_type_records if r["final_category"] == "confirmed_match")
                incorrect = sum(1 for r in store_type_records if r["final_category"] == "classification_error")
                missing = sum(1 for r in store_type_records if r["final_category"] in ["missing_dialog_text_found", "missing_dialog_no_text"])
                false_positives = sum(1 for r in store_type_records if r["final_category"] == "false_positive")
                
                accuracy = (correct / (correct + incorrect) * 100) if (correct + incorrect) > 0 else None
                
                # Examples of errors
                error_examples = []
                for r in store_type_records:
                    if r["final_category"] in ["classification_error", "false_positive"]:
                        error_examples.append(f"{r['start_time']}: {r['human_comment'][:50]}")
                examples_str = "; ".join(error_examples[:3]) if error_examples else "-"
                
                store_type_stats.append({
                    "Магазин": store,
                    "Цель визита": dialog_type,
                    "Автоматических диалогов": len(store_type_records),
                    "С оценкой": sum(1 for r in store_type_records if r["human_comment"]),
                    "Совпадений": correct,
                    "Несовпадений": incorrect,
                    "Пропусков": missing,
                    "Ложных срабатываний": false_positives,
                    "Accuracy (%)": f"{accuracy:.1f}" if accuracy else "-",
                    "Примеры ошибок": examples_str,
                    "Характер проблемы": "Классификация" if incorrect > 0 else "Пропуски" if missing > 0 else "Ложные" if false_positives > 0 else "-"
                })
        
        df_store_type = pd.DataFrame(store_type_stats)
        df_store_type.to_excel(writer, sheet_name="Магазин × цель", index=False)
        
        # ЛИСТ 4: Магазин × миссия
        print("  Лист 4: Магазин × миссия...")
        
        # В этом проекте миссия = диалог (client_N)
        mission_stats = []
        for store in sorted(df_auto['store_id'].unique()):
            store_records = [r for r in all_records if r["store_id"] == store]
            
            for record in store_records:
                mission_stats.append({
                    "Магазин": record["store_id"],
                    "ID миссии": record["client_id"],
                    "Название миссии": f"{record['human_dialog_type'] or record['auto_dialog_type']}",
                    "Цель визита": record["human_dialog_type"] or record["auto_dialog_type"],
                    "ID диалога": record["auto_dialog_id"] or record["manual_dialog_id"][:8] if record["manual_dialog_id"] else "-",
                    "Авто результат": f"{record['auto_dialog_type']}, sale={record['auto_is_sale']}" if record["auto_dialog_type"] else "-",
                    "Человек результат": f"{record['human_dialog_type']}, {record['human_is_sale']}" if record["human_dialog_type"] else "-",
                    "Совпадение": "Да" if record["final_category"] == "confirmed_match" else "Нет" if record["final_category"] in ["classification_error", "false_positive"] else "Н/О",
                    "Тип расхождения": record["error_reason"] or "-",
                    "Текст в БД": "Да" if record["has_text_in_db"] else "Нет",
                    "Причина": record["error_reason"] or "-",
                    "Рекомендация": "Проверить segmentation" if record["final_category"] == "missing_dialog_text_found" else "Проверить аудио/VAD" if record["final_category"] == "missing_dialog_no_text" else "-"
                })
        
        df_mission = pd.DataFrame(mission_stats)
        df_mission.to_excel(writer, sheet_name="Магазин × миссия", index=False)
        
        # ЛИСТ 5: Все диалоги
        print("  Лист 5: Все диалоги...")
        
        df_all = pd.DataFrame(all_records)
        df_all.to_excel(writer, sheet_name="Все диалоги", index=False)
        
        # ЛИСТ 6: Несовпадения
        print("  Лист 6: Несовпадения...")
        
        mismatches = [r for r in all_records if r["final_category"] in ["classification_error", "false_positive", "missing_dialog_text_found", "missing_dialog_no_text"]]
        
        mismatch_details = []
        for r in mismatches:
            # Determine error stage
            if r["final_category"] == "false_positive":
                error_stage = "Сегментация/Классификация"
                fix = "Улучшить детекцию внутренних разговоров"
            elif r["final_category"] == "missing_dialog_text_found":
                error_stage = "Сегментация"
                fix = "Проверить gap thresholds, VAD boundaries"
            elif r["final_category"] == "missing_dialog_no_text":
                error_stage = "Аудио/VAD/ASR"
                fix = "Проверить аудио файлы, VAD логи, ASR"
            else:
                error_stage = "Классификация"
                fix = "Проверить промпт классификации"
            
            mismatch_details.append({
                "Магазин": r["store_id"],
                "Миссия": r["client_id"],
                "Диалог": r["auto_dialog_id"] or r["manual_dialog_id"][:8] if r["manual_dialog_id"] else "-",
                "Авто результат": f"{r['auto_dialog_type']}, sale={r['auto_is_sale']}" if r["auto_dialog_type"] else "-",
                "Человек результат": f"{r['human_dialog_type']}, {r['human_is_sale']}" if r["human_dialog_type"] else "-",
                "Комментарий": r["human_comment"],
                "Основание": r["error_reason"],
                "Этап": error_stage,
                "Исправление": fix,
                "Уверенность": "Высокая" if r["human_comment"] else "Средняя"
            })
        
        df_mismatch = pd.DataFrame(mismatch_details)
        df_mismatch.to_excel(writer, sheet_name="Несовпадения", index=False)
        
        # ЛИСТ 7: Ручные пропуски
        print("  Лист 7: Ручные пропуски...")
        
        manual_records = [r for r in all_records if r["client_id"] == "Manual"]
        
        manual_details = []
        for r in manual_records:
            if r["final_category"] == "missing_dialog_text_found":
                cause = "Сегментация не сформировала диалог"
                stage = "Segmentation"
                next_step = "Проверить gap thresholds, VAD boundaries"
            else:
                cause = "Текст не найден в БД"
                stage = "Аудио/VAD/ASR"
                next_step = "Проверить аудио файлы, VAD логи, ASR ошибки"
            
            manual_details.append({
                "Магазин": r["store_id"],
                "ID ручного диалога": r["manual_dialog_id"][:8] if r["manual_dialog_id"] else "-",
                "Время": f"{r['start_time']} - {r['end_time']}",
                "Миссия": r["human_dialog_type"],
                "Цель": r["human_is_sale"],
                "Текст ручного": r["recognition_text"],
                "Текст в БД": "Да" if r["has_text_in_db"] else "Нет",
                "ID записи": r["note"],
                "Временной интервал": f"{r['start_time']} - {r['end_time']}",
                "Причина": cause,
                "Этап": stage,
                "Следующий шаг": next_step
            })
        
        df_manual = pd.DataFrame(manual_details)
        df_manual.to_excel(writer, sheet_name="Ручные пропуски", index=False)
        
        # ЛИСТ 8: Ложные срабатывания
        print("  Лист 8: Ложные срабатывания...")
        
        fp_records = [r for r in all_records if r["final_category"] == "false_positive"]
        
        fp_details = []
        for r in fp_records:
            fp_details.append({
                "Магазин": r["store_id"],
                "ID диалога": r["auto_dialog_id"],
                "Время": f"{r['start_time']} - {r['end_time']}",
                "Авто запись": f"{r['auto_dialog_type']}, sale={r['auto_is_sale']}",
                "Комментарий": r["human_comment"],
                "Текст": r["recognition_text"][:200],
                "Почему FP": "Человек указал 'клиента не было'",
                "Причина": "Внутренний разговор или технический диалог",
                "Рекомендация": "Улучшить детекцию внутренних разговоров"
            })
        
        df_fp = pd.DataFrame(fp_details)
        df_fp.to_excel(writer, sheet_name="Ложные срабатывания", index=False)
        
        # ЛИСТ 9: Неоднозначные
        print("  Лист 9: Неоднозначные...")
        
        ambiguous_records = [r for r in all_records if r["final_category"] in ["unknown", "ambiguous", "no_human_evaluation"]]
        
        ambiguous_details = []
        for r in ambiguous_records[:50]:  # Limit to first 50
            ambiguous_details.append({
                "Магазин": r["store_id"],
                "ID": r["auto_dialog_id"] or r["manual_dialog_id"][:8] if r["manual_dialog_id"] else "-",
                "Время": f"{r['start_time']} - {r['end_time']}",
                "Проблема": "Нет человеческой оценки" if r["final_category"] == "no_human_evaluation" else "Не удалось интерпретировать",
                "Данные для разрешения": "Требуется ручная проверка" if r["final_category"] == "no_human_evaluation" else "Уточнить формат комментария"
            })
        
        df_ambiguous = pd.DataFrame(ambiguous_details)
        df_ambiguous.to_excel(writer, sheet_name="Неоднозначные", index=False)
        
        # ЛИСТ 10: Методика
        print("  Лист 10: Методика...")
        
        methodology = [
            ["ИСТОЧНИКИ ДАННЫХ", ""],
            ["Файл автоматических диалогов", f"reports/final_transcript_report_{DATE}.xlsx"],
            ["Файл комментариев", "reports/comments.json"],
            ["Файл ручных диалогов", "reports/manual_dialogs.json"],
            ["Результаты проверки текста", "reports/manual_dialogs_text_check_2026-10-02.json"],
            ["", ""],
            ["ПРАВИЛА СОПОСТАВЛЕНИЯ", ""],
            ["Ключ сопоставления", f"DATE_STORE_client_N (например: {DATE}_г_Ставрополь_ул_50_лет_ВЛКСМ_43_client_1)"],
            ["Ручные диалоги", "Хранятся отдельно в manual_dialogs.json, client_id='Manual'"],
            ["", ""],
            ["ФОРМАТЫ КОММЕНТАРИЕВ", ""],
            ["1.+ 2.+", "Оба признака правильные (dialog_type и is_sale)"],
            ["1.- 2.-", "Оба признака неправильные"],
            ["1.тип 2.итог", "Детальное описание (например: 1.покупка 2.покупка)"],
            ["клиента не было", "Ложное срабатывание (не было клиента)"],
            ["пропущенный клиент", "Ручной диалог (пропущен системой)"],
            ["", ""],
            ["ПРАВИЛА ПОДСЧЁТА METRICS", ""],
            ["Accuracy", "Правильные / (Правильные + Неправильные) × 100% (только для бинарных оценок)"],
            ["False Positive Rate", "Ложные срабатывания / Всего проверено × 100%"],
            ["Missed Dialogs", "Ручные диалоги, добавленные пользователем"],
            ["", ""],
            ["КАТЕГОРИИ", ""],
            ["confirmed_match", "Подтверждённое совпадение"],
            ["classification_error", "Подтверждённое несовпадение"],
            ["false_positive", "Ложное срабатывание"],
            ["missing_dialog_text_found", "Пропущенный диалог (текст в БД)"],
            ["missing_dialog_no_text", "Пропущенный диалог (текста нет)"],
            ["no_human_evaluation", "Нет человеческой оценки"],
            ["unknown", "Неоднозначное сопоставление"],
            ["", ""],
            ["ОГРАНИЧЕНИЯ", ""],
            ["Миссии", "В этом проекте миссия = диалог (client_N)"],
            ["Текст в БД", "Проверка по таблице transcripts (store_id, created_at)"],
            ["Причины ошибок", "Указываются на основе анализа, не предполагаются"],
            ["", ""],
            ["КОНТРОЛЬНЫЕ ЧИСЛА", ""],
            ["Автоматических диалогов", len(df_auto)],
            ["Магазинов", len(df_auto['store_id'].unique())],
            ["Комментариев", len(comments_10_02)],
            ["Ручных диалогов", len(manual_10_02) - 1],  # Exclude test_store
            ["С текстом в БД", len(text_check.get("has_text", []))],
            ["Без текста в БД", len(text_check.get("no_text", []))],
            ["Ложных срабатываний", sum(1 for r in all_records if r["final_category"] == "false_positive")]
        ]
        
        df_methodology = pd.DataFrame(methodology, columns=["Параметр", "Значение"])
        df_methodology.to_excel(writer, sheet_name="Методика", index=False)
        
        # Format Excel
        workbook = writer.book
        for sheet_name in writer.sheets:
            worksheet = writer.sheets[sheet_name]
            worksheet.freeze_panes = 'A2'  # Freeze first row
            for column in worksheet.columns:
                for cell in column:
                    cell.number_format = 'General'
        
        print(f"\n✅ Отчёт сохранён: {output_file}")
        
        # Print summary
        print("\n" + "=" * 80)
        print("КОНТРОЛЬНЫЕ ЧИСЛА")
        print("=" * 80)
        print(f"Автоматических диалогов: {len(df_auto)}")
        print(f"Магазинов: {len(df_auto['store_id'].unique())}")
        print(f"Комментариев: {len(comments_10_02)}")
        print(f"Ручных диалогов (без test_store): {len([r for r in all_records if r['client_id'] == 'Manual'])}")
        print(f"С текстом в БД: {len(text_check.get('has_text', []))}")
        print(f"Без текста в БД: {len(text_check.get('no_text', []))}")
        print(f"Ложных срабатываний: {sum(1 for r in all_records if r['final_category'] == 'false_positive')}")
        print(f"Подтверждённых совпадений: {sum(1 for r in all_records if r['final_category'] == 'confirmed_match')}")
        print(f"Ошибок классификации: {sum(1 for r in all_records if r['final_category'] == 'classification_error')}")
        
        print("\nЛИСТЫ ОТЧЁТА:")
        print(f"  1. Сводка - {len(df_summary)} строк")
        print(f"  2. По магазинам - {len(df_stores)} строк")
        print(f"  3. Магазин × цель - {len(df_store_type)} строк")
        print(f"  4. Магазин × миссия - {len(df_mission)} строк")
        print(f"  5. Все диалоги - {len(df_all)} строк")
        print(f"  6. Несовпадения - {len(df_mismatch)} строк")
        print(f"  7. Ручные пропуски - {len(df_manual)} строк")
        print(f"  8. Ложные срабатывания - {len(df_fp)} строк")
        print(f"  9. Неоднозначные - {len(df_ambiguous)} строк")
        print(f"  10. Методика - {len(df_methodology)} строк")

if __name__ == "__main__":
    main()
