#!/usr/bin/env python3
"""
TIMELINE_GT_AI отчёт для 02.10.2026, организованный по магазинам.

Структура:
- Каждый магазин — отдельный лист
- Хронологический порядок
- Три части: GT (слева), Сравнение (центр), AI (справа)

Источники данных:
- comments.json: GT-оценки для AI диалогов (ключ: "YYYY-MM-DD_store_client_X")
- manual_dialogs.json: GT диалоги, пропущенные AI
- final_transcript_report_2026-10-02.xlsx: AI-сегментация
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment


# Paths
BASE_DIR = Path(__file__).resolve().parents[3]
REPORTS_DIR = BASE_DIR / "reports"
FINAL_REPORT = REPORTS_DIR / "final_transcript_report_2026-10-02.xlsx"
COMMENTS_FILE = REPORTS_DIR / "comments.json"
MANUAL_DIALOGS_FILE = REPORTS_DIR / "manual_dialogs.json"
OUTPUT_FILE = BASE_DIR / "tests" / "human_gt" / "2026-10-02" / "TIMELINE_GT_AI_2026-10-02.xlsx"

ANALYSIS_DATE = "2026-10-02"


def parse_comment(comment: str) -> dict:
    """Parse human comment to extract GT evaluation."""
    result = {
        "raw": comment or "",
        "is_false_positive": False,
        "is_correct": False,
        "is_error": False,
        "is_ambiguous": False,
        "is_factual_evaluation": False,  # Contains factual description like "1. сервис 2. нет покупки"
        "mission_confirmed": None,
        "outcome_confirmed": None,
        "mission_description": None,  # Extracted mission description from comment
        "outcome_description": None,  # Extracted outcome description from comment
    }
    
    if not comment:
        return result
    
    comment_lower = comment.lower().strip()
    
    # False positive: "клиента не было"
    if "клиента не было" in comment_lower:
        result["is_false_positive"] = True
        return result
    
    # Check for explicit corrections in format "1.+ 2.+" or "1.- 2.-"
    has_1_plus = "1.+" in comment_lower or "1. +" in comment_lower
    has_1_minus = "1.-" in comment_lower or "1. -" in comment_lower
    has_2_plus = "2.+" in comment_lower or "2. +" in comment_lower
    has_2_minus = "2.-" in comment_lower or "2. -" in comment_lower
    
    # Determine mission status
    if has_1_plus:
        result["mission_confirmed"] = "CORRECT"
    elif has_1_minus:
        result["mission_confirmed"] = "INCORRECT"
    
    # Determine outcome status
    if has_2_plus:
        result["outcome_confirmed"] = "CORRECT"
    elif has_2_minus:
        result["outcome_confirmed"] = "INCORRECT"
    
    # Check for factual evaluation (e.g., "1. сервис 2. нет покупки")
    # This is when comment describes the actual situation without +/- markers
    if not (has_1_plus or has_1_minus or has_2_plus or has_2_minus):
        # Try to extract factual description
        if "1." in comment_lower and "2." in comment_lower:
            result["is_factual_evaluation"] = True
            # Extract mission description (between "1." and "2.")
            import re
            match = re.search(r'1\.\s*(.*?)\s*2\.', comment, re.IGNORECASE)
            if match:
                result["mission_description"] = match.group(1).strip()
            match = re.search(r'2\.\s*(.*)', comment, re.IGNORECASE)
            if match:
                result["outcome_description"] = match.group(1).strip()
    
    # Overall classification
    if has_1_plus and has_2_plus:
        result["is_correct"] = True
    elif has_1_minus or has_2_minus:
        result["is_error"] = True
    elif "неоднознач" in comment_lower or "сомнител" in comment_lower:
        result["is_ambiguous"] = True
    
    return result


def time_to_minutes(time_str: str) -> float:
    """Convert time string to minutes for sorting."""
    try:
        if isinstance(time_str, pd.Timestamp):
            return time_str.hour * 60 + time_str.minute + time_str.second / 60
        elif isinstance(time_str, str):
            parts = time_str.split(":")
            if len(parts) >= 2:
                hours = int(parts[0])
                minutes = int(parts[1])
                seconds = int(parts[2]) if len(parts) > 2 else 0
                return hours * 60 + minutes + seconds / 60
        return 0
    except Exception:
        return 0


def load_data():
    """Load all source data."""
    # Load final report (AI segmentation)
    df_final = pd.read_excel(FINAL_REPORT)
    
    # Load comments (GT evaluations for AI dialogs)
    with open(COMMENTS_FILE) as f:
        comments = json.load(f)
    
    # Filter comments for analysis date
    comments_filtered = {}
    for key, value in comments.items():
        if ANALYSIS_DATE in key:
            comments_filtered[key] = value
    
    # Load manual dialogs (GT dialogs missed by AI)
    with open(MANUAL_DIALOGS_FILE) as f:
        manual_dialogs = json.load(f)
    
    # Filter manual dialogs for analysis date
    manual_filtered = [d for d in manual_dialogs if d.get("date") == ANALYSIS_DATE]
    
    return df_final, comments_filtered, manual_filtered


def build_timeline_for_store(store_id: str, df_final: pd.DataFrame,
                            comments: dict, manual_dialogs: list) -> tuple[list, dict]:
    """
    Build timeline data for a single store.
    
    Returns:
        tuple: (timeline_rows, stats)
    """
    # Get AI dialogs for this store
    ai_dialogs = df_final[df_final["store_id"] == store_id].to_dict("records")
    
    # Get comments for this store (GT evaluations)
    # Key format: "2026-10-02_store_client_X"
    # Store names may differ slightly (e.g., "37" vs "378"), so use flexible matching
    store_comments = {}
    for key, comment in comments.items():
        # Check if this key belongs to the current store
        # Extract store name from key (between date and client/Manual)
        parts = key.split("_")
        if len(parts) >= 3 and parts[0] == ANALYSIS_DATE:
            # Reconstruct store name from parts until we hit "client" or "Manual"
            store_parts = []
            for i, part in enumerate(parts[1:], 1):
                if part in ["client", "Manual"]:
                    break
                store_parts.append(part)
            
            key_store = "_".join(store_parts)
            
            # Flexible matching: check if store_id matches key_store (either way)
            if store_id in key_store or key_store in store_id:
                # Extract client_id from key
                try:
                    client_idx = parts.index("client")
                    if client_idx + 1 < len(parts):
                        client_id = f"client_{parts[client_idx + 1]}"
                        store_comments[client_id] = {
                            "raw_comment": comment,
                            "parsed": parse_comment(comment)
                        }
                except ValueError:
                    pass
    
    # Get manual dialogs for this store (missed by AI)
    # Store names may differ slightly, so use substring matching
    store_manual = []
    for md in manual_dialogs:
        md_store = md.get("store_id", "")
        # Check if store names match (accounting for truncation)
        if md_store == store_id or md_store.startswith(store_id) or store_id.startswith(md_store):
            store_manual.append(md)
    
    # Build timeline events
    timeline_rows = []
    
    # Process AI dialogs with their GT comments
    for ai_dialog in ai_dialogs:
        ai_id = ai_dialog.get("client_id", "")
        ai_start_time = str(ai_dialog.get("dialog_start_at", ""))[:16] if pd.notna(ai_dialog.get("dialog_start_at")) else ""
        ai_end_time = str(ai_dialog.get("dialog_end_at", ""))[:16] if pd.notna(ai_dialog.get("dialog_end_at")) else ""
        
        # Get GT comment for this AI dialog
        comment_data = store_comments.get(ai_id, {})
        raw_comment = comment_data.get("raw_comment", "")
        parsed_comment = comment_data.get("parsed", parse_comment(""))
        
        # Build AI text
        ai_text_lines = [f"{ai_id}", store_id, f"{ai_start_time} - {ai_end_time}"]
        if ai_dialog.get("dialog_type"):
            ai_text_lines.append(f"Миссия: {ai_dialog.get('dialog_type')}")
        if ai_dialog.get("is_sale"):
            ai_text_lines.append("Покупка: Да")
        else:
            ai_text_lines.append("Покупка: Нет")
        ai_text = "\n".join(ai_text_lines)
        
        # Build record note (explanation for the record)
        record_note = ""
        
        # Determine match status and build GT text
        if parsed_comment["is_false_positive"]:
            # False positive: AI found dialog but no client existed
            match_status = "Ложное срабатывание"
            explanation = "Комментарий: 'клиента не было'"
            gt_text = ""
            gt_id = ""
            gt_mission = ""
            gt_outcome = ""
            record_note = "AI обнаружил диалог, но в реальности клиента не было"
            
        elif parsed_comment["is_correct"]:
            # Correct: AI and GT agree
            match_status = "Совпадение"
            explanation = "AI и GT согласны"
            gt_text_lines = [f"GT ({ai_id})", store_id, ai_start_time]
            gt_text_lines.append(f"Оценка: {raw_comment}")
            gt_text = "\n".join(gt_text_lines)
            gt_id = ai_id
            gt_mission = "Подтверждено"
            gt_outcome = "Подтверждено"
            record_note = "AI классификация подтверждена ручной разметкой"
            
        elif parsed_comment["is_error"]:
            # Error: AI and GT disagree on classification
            match_status = "Ошибка AI"
            explanation = f"Расхождение в классификации: {raw_comment}"
            gt_text_lines = [f"GT ({ai_id})", store_id, ai_start_time]
            gt_text_lines.append(f"Оценка: {raw_comment}")
            gt_text = "\n".join(gt_text_lines)
            gt_id = ai_id
            gt_mission = "Оспорено" if parsed_comment["mission_confirmed"] == "INCORRECT" else "Подтверждено"
            gt_outcome = "Оспорено" if parsed_comment["outcome_confirmed"] == "INCORRECT" else "Подтверждено"
            record_note = "Ручная разметка оспаривает AI классификацию"
            
        elif parsed_comment["is_factual_evaluation"]:
            # Factual evaluation: comment describes actual situation (e.g., "1. сервис 2. нет покупки")
            # This is NOT "insufficient data" - it's a real evaluation
            match_status = "Совпадение"  # Default to correct unless we detect mismatch
            gt_text_lines = [f"GT ({ai_id})", store_id, ai_start_time]
            gt_text_lines.append(f"Оценка: {raw_comment}")
            gt_text = "\n".join(gt_text_lines)
            gt_id = ai_id
            
            # Extract mission and outcome from comment
            if parsed_comment["mission_description"]:
                gt_mission = parsed_comment["mission_description"]
            else:
                gt_mission = "Описано в комментарии"
            
            if parsed_comment["outcome_description"]:
                gt_outcome = parsed_comment["outcome_description"]
            else:
                gt_outcome = "Описано в комментарии"
            
            explanation = f"Фактическая оценка: {raw_comment}"
            record_note = "Комментарий содержит фактическое описание ситуации"
            
        elif parsed_comment["is_ambiguous"]:
            # Ambiguous: unclear classification
            match_status = "Неоднозначно"
            explanation = f"Неоднозначный комментарий: {raw_comment}"
            gt_text_lines = [f"GT ({ai_id})", store_id, ai_start_time]
            gt_text_lines.append(f"Оценка: {raw_comment}")
            gt_text = "\n".join(gt_text_lines)
            gt_id = ai_id
            gt_mission = "Неясно"
            gt_outcome = "Неясно"
            record_note = "Комментарий содержит неоднозначную оценку"
            
        else:
            # No comment: cannot validate
            match_status = "Недостаточно данных"
            explanation = "Нет человеческой разметки"
            gt_text = ""
            gt_id = ""
            gt_mission = ""
            gt_outcome = ""
            record_note = "Отсутствует комментарий для данного AI диалога"
        
        timeline_rows.append({
            "time": ai_start_time,
            "time_minutes": time_to_minutes(ai_start_time),
            "gt_text": gt_text,
            "gt_id": gt_id,
            "gt_mission": gt_mission,
            "gt_outcome": gt_outcome,
            "match_status": match_status,
            "explanation": explanation,
            "ai_text": ai_text,
            "ai_id": ai_id,
            "ai_mission": ai_dialog.get("dialog_type", ""),
            "ai_outcome": "Покупка" if ai_dialog.get("is_sale") else "Нет покупки",
            "comment": raw_comment,
            "record_note": record_note  # NEW: explanation for the record
        })
    
    # Add manual dialogs (missed by AI)
    for gt_dialog in store_manual:
        gt_start_time = gt_dialog.get("start_time", "")
        gt_id = gt_dialog.get("id", f"Manual_{gt_start_time}")
        
        gt_text_lines = [gt_id, store_id, gt_start_time]
        if gt_dialog.get("dialog_type"):
            gt_text_lines.append(f"Цель: {gt_dialog.get('dialog_type')}")
        if gt_dialog.get("result"):
            gt_text_lines.append(f"Результат: {gt_dialog.get('result')}")
        gt_text = "\n".join(gt_text_lines)
        
        timeline_rows.append({
            "time": gt_start_time,
            "time_minutes": time_to_minutes(gt_start_time),
            "gt_text": gt_text,
            "gt_id": gt_id,
            "gt_mission": gt_dialog.get("dialog_type", ""),
            "gt_outcome": gt_dialog.get("result", ""),
            "match_status": "AI пропустил",
            "explanation": "Диалог в GT, но не найден AI",
            "ai_text": "",
            "ai_id": "",
            "ai_mission": "",
            "ai_outcome": "",
            "comment": "Ручной диалог",
            "record_note": "Диалог присутствует в ручной разметке, но не обнаружен AI"
        })
    
    # Sort by time
    timeline_rows.sort(key=lambda x: x["time_minutes"])
    
    # Calculate stats
    stats = {
        "total_gt": sum(1 for r in timeline_rows if r["gt_text"]),
        "total_ai": len(ai_dialogs),
        "correct": sum(1 for r in timeline_rows if r["match_status"] == "Совпадение"),
        "error": sum(1 for r in timeline_rows if r["match_status"] == "Ошибка AI"),
        "missed": sum(1 for r in timeline_rows if r["match_status"] == "AI пропустил"),
        "false_positive": sum(1 for r in timeline_rows if r["match_status"] == "Ложное срабатывание"),
        "ambiguous": sum(1 for r in timeline_rows if r["match_status"] == "Неоднозначно"),
        "insufficient": sum(1 for r in timeline_rows if r["match_status"] == "Недостаточно данных")
    }
    
    return timeline_rows, stats


def create_excel_report(df_final: pd.DataFrame, comments: dict, manual_dialogs: list):
    """Create the timeline Excel report organized by store."""
    # Create workbook
    wb = Workbook()
    default_sheet = wb.active
    wb.remove(default_sheet)
    
    # Get all stores from AI report
    all_stores = df_final["store_id"].unique().tolist()
    
    # Classify stores: included (has human annotation) vs excluded
    included_stores = []
    excluded_stores = []
    
    for store in all_stores:
        # Check if store has comments
        store_comments = [k for k in comments.keys() if store in k]
        
        # Check if store has manual dialogs
        store_manual = []
        for md in manual_dialogs:
            md_store = md.get("store_id", "")
            if md_store == store or md_store.startswith(store) or store.startswith(md_store):
                store_manual.append(md)
        
        if store_comments or store_manual:
            included_stores.append(store)
        else:
            excluded_stores.append((store, "Нет человеческой разметки"))
    
    # Build timeline for each included store
    store_data = {}
    for store in included_stores:
        timeline_rows, stats = build_timeline_for_store(store, df_final, comments, manual_dialogs)
        store_data[store] = {
            "timeline": timeline_rows,
            "stats": stats
        }
    
    # Create "Сводка" sheet
    summary_sheet = wb.create_sheet("Сводка")
    
    summary_headers = [
        "Магазин", "GT записей", "AI диалогов",
        "Совпадений", "Ошибок AI", "Пропусков AI",
        "Ложных срабатываний", "Неоднозначно", "Недостаточно данных"
    ]
    
    for col_idx, header in enumerate(summary_headers, 1):
        cell = summary_sheet.cell(row=1, column=col_idx, value=header)
        cell.font = Font(bold=True)
        cell.fill = PatternFill(start_color="DDDDDD", fill_type="solid")
    
    for row_idx, store in enumerate(included_stores, 2):
        stats = store_data[store]["stats"]
        summary_sheet.cell(row=row_idx, column=1, value=store)
        summary_sheet.cell(row=row_idx, column=2, value=stats["total_gt"])
        summary_sheet.cell(row=row_idx, column=3, value=stats["total_ai"])
        summary_sheet.cell(row=row_idx, column=4, value=stats["correct"])
        summary_sheet.cell(row=row_idx, column=5, value=stats["error"])
        summary_sheet.cell(row=row_idx, column=6, value=stats["missed"])
        summary_sheet.cell(row=row_idx, column=7, value=stats["false_positive"])
        summary_sheet.cell(row=row_idx, column=8, value=stats["ambiguous"])
        summary_sheet.cell(row=row_idx, column=9, value=stats["insufficient"])
    
    # Totals row
    totals_row = len(included_stores) + 2
    summary_sheet.cell(row=totals_row, column=1, value="ВСЕГО")
    summary_sheet.cell(row=totals_row, column=2, value=sum(store_data[s]["stats"]["total_gt"] for s in included_stores))
    summary_sheet.cell(row=totals_row, column=3, value=sum(store_data[s]["stats"]["total_ai"] for s in included_stores))
    summary_sheet.cell(row=totals_row, column=4, value=sum(store_data[s]["stats"]["correct"] for s in included_stores))
    summary_sheet.cell(row=totals_row, column=5, value=sum(store_data[s]["stats"]["error"] for s in included_stores))
    summary_sheet.cell(row=totals_row, column=6, value=sum(store_data[s]["stats"]["missed"] for s in included_stores))
    summary_sheet.cell(row=totals_row, column=7, value=sum(store_data[s]["stats"]["false_positive"] for s in included_stores))
    summary_sheet.cell(row=totals_row, column=8, value=sum(store_data[s]["stats"]["ambiguous"] for s in included_stores))
    summary_sheet.cell(row=totals_row, column=9, value=sum(store_data[s]["stats"]["insufficient"] for s in included_stores))
    
    for col_idx in range(1, 10):
        cell = summary_sheet.cell(row=totals_row, column=col_idx)
        cell.font = Font(bold=True)
    
    # Auto-adjust column widths
    column_widths = {1: 40, 2: 12, 3: 12, 4: 12, 5: 12, 6: 12, 7: 18, 8: 12, 9: 18}
    for col_idx, width in column_widths.items():
        summary_sheet.column_dimensions[chr(64 + col_idx)].width = width
    
    # Create sheets for each included store
    for store in included_stores:
        data = store_data[store]
        timeline = data["timeline"]
        stats = data["stats"]
        
        # Create sheet name (truncate if too long)
        sheet_name = store[:31]
        if len(store) > 31:
            sheet_name = store[:28] + "..."
        
        sheet = wb.create_sheet(sheet_name)
        
        # Header with stats
        sheet.cell(row=1, column=1, value=f"Магазин: {store}")
        sheet.cell(row=1, column=1).font = Font(bold=True, size=14)
        sheet.merge_cells(f"A1:L1")
        
        sheet.cell(row=2, column=1, value=f"Дата: {ANALYSIS_DATE}")
        sheet.cell(row=3, column=1, value=f"GT записей: {stats['total_gt']}")
        sheet.cell(row=4, column=1, value=f"AI диалогов: {stats['total_ai']}")
        sheet.cell(row=5, column=1, value=f"Совпадений: {stats['correct']}")
        sheet.cell(row=6, column=1, value=f"Ошибок AI: {stats['error']}")
        sheet.cell(row=7, column=1, value=f"Пропусков AI: {stats['missed']}")
        sheet.cell(row=8, column=1, value=f"Ложных срабатываний: {stats['false_positive']}")
        sheet.cell(row=9, column=1, value=f"Неоднозначно: {stats['ambiguous']}")
        sheet.cell(row=10, column=1, value=f"Недостаточно данных: {stats['insufficient']}")
        
        # Column headers (row 12)
        headers = [
            "Время", "GT: текст", "GT: ID", "GT: цель", "GT: результат",
            "Сравнение: статус", "Сравнение: объяснение",
            "AI: текст", "AI: ID", "AI: миссия", "AI: результат",
            "Комментарий", "Комментарий к записи"
        ]
        
        for col_idx, header in enumerate(headers, 1):
            cell = sheet.cell(row=12, column=col_idx, value=header)
            cell.font = Font(bold=True)
            cell.fill = PatternFill(start_color="DDDDDD", fill_type="solid")
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        
        # Data rows
        for row_idx, row_data in enumerate(timeline, 13):
            sheet.cell(row=row_idx, column=1, value=row_data.get("time", ""))
            sheet.cell(row=row_idx, column=2, value=row_data.get("gt_text", ""))
            sheet.cell(row=row_idx, column=3, value=row_data.get("gt_id", ""))
            sheet.cell(row=row_idx, column=4, value=row_data.get("gt_mission", ""))
            sheet.cell(row=row_idx, column=5, value=row_data.get("gt_outcome", ""))
            
            match_status = row_data.get("match_status", "")
            sheet.cell(row=row_idx, column=6, value=match_status)
            
            # Color match status
            match_cell = sheet.cell(row=row_idx, column=6)
            if match_status == "Совпадение":
                match_cell.fill = PatternFill(start_color="C6EFCE", fill_type="solid")
            elif match_status == "Ошибка AI":
                match_cell.fill = PatternFill(start_color="FFC7CE", fill_type="solid")
            elif match_status == "AI пропустил":
                match_cell.fill = PatternFill(start_color="FFEB9C", fill_type="solid")
            elif match_status == "Ложное срабатывание":
                match_cell.fill = PatternFill(start_color="FFCC99", fill_type="solid")
            elif match_status == "Неоднозначно":
                match_cell.fill = PatternFill(start_color="D9D9D9", fill_type="solid")
            elif match_status == "Недостаточно данных":
                match_cell.fill = PatternFill(start_color="EFEFEF", fill_type="solid")
            
            sheet.cell(row=row_idx, column=7, value=row_data.get("explanation", ""))
            sheet.cell(row=row_idx, column=8, value=row_data.get("ai_text", ""))
            sheet.cell(row=row_idx, column=9, value=row_data.get("ai_id", ""))
            sheet.cell(row=row_idx, column=10, value=row_data.get("ai_mission", ""))
            sheet.cell(row=row_idx, column=11, value=row_data.get("ai_outcome", ""))
            sheet.cell(row=row_idx, column=12, value=row_data.get("comment", ""))
            sheet.cell(row=row_idx, column=13, value=row_data.get("record_note", ""))
        
        # Auto-adjust column widths
        column_widths = {
            1: 12, 2: 45, 3: 15, 4: 20, 5: 18,
            6: 20, 7: 35,
            8: 45, 9: 15, 10: 20, 11: 18,
            12: 30, 13: 40
        }
        for col_idx, width in column_widths.items():
            sheet.column_dimensions[chr(64 + col_idx)].width = width
        
        # Wrap text for long columns
        for col_idx in [2, 7, 8, 12, 13]:
            for row_idx in range(13, 13 + len(timeline) + 1):
                sheet.cell(row=row_idx, column=col_idx).alignment = Alignment(wrap_text=True)
        
        # Freeze panes
        sheet.freeze_panes = "A13"
    
    # Create "Исключённые магазины" sheet
    excluded_sheet = wb.create_sheet("Исключённые")
    
    excluded_headers = [
        "Магазин", "AI диалогов", "GT комментариев",
        "GT ручных", "Причина исключения"
    ]
    
    for col_idx, header in enumerate(excluded_headers, 1):
        cell = excluded_sheet.cell(row=1, column=col_idx, value=header)
        cell.font = Font(bold=True)
        cell.fill = PatternFill(start_color="DDDDDD", fill_type="solid")
    
    for row_idx, (store, reason) in enumerate(excluded_stores, 2):
        ai_count = len(df_final[df_final["store_id"] == store])
        store_comments = len([k for k in comments.keys() if store in k])
        
        store_manual = []
        for md in manual_dialogs:
            md_store = md.get("store_id", "")
            if md_store == store or md_store.startswith(store) or store.startswith(md_store):
                store_manual.append(md)
        
        excluded_sheet.cell(row=row_idx, column=1, value=store)
        excluded_sheet.cell(row=row_idx, column=2, value=ai_count)
        excluded_sheet.cell(row=row_idx, column=3, value=store_comments)
        excluded_sheet.cell(row=row_idx, column=4, value=len(store_manual))
        excluded_sheet.cell(row=row_idx, column=5, value=reason)
    
    excluded_sheet.column_dimensions["A"].width = 40
    excluded_sheet.column_dimensions["B"].width = 15
    excluded_sheet.column_dimensions["C"].width = 15
    excluded_sheet.column_dimensions["D"].width = 15
    excluded_sheet.column_dimensions["E"].width = 30
    
    # Create "Методика" sheet
    methodology_sheet = wb.create_sheet("Методика")
    
    methodology_text = [
        ["ИСТОЧНИКИ ДАННЫХ", ""],
        ["Финальный отчёт AI", str(FINAL_REPORT)],
        ["GT комментарии", str(COMMENTS_FILE)],
        ["GT ручные диалоги", str(MANUAL_DIALOGS_FILE)],
        ["", ""],
        ["ПРАВИЛА СОПОСТАВЛЕНИЯ", ""],
        ["1. Комментарии (comments.json) — это GT-оценки для AI диалогов", ""],
        ["2. Ключ формата: 'YYYY-MM-DD_store_client_X'", ""],
        ["3. Ручные диалоги (manual_dialogs.json) — это GT, пропущенные AI", ""],
        ["4. Сопоставление по client_id и времени начала диалога", ""],
        ["", ""],
        ["СТАТУСЫ СОПОСТАВЛЕНИЯ", ""],
        ["Совпадение", "AI и GT согласны (1.+ 2.+)"],
        ["Ошибка AI", "Расхождение в классификации (1.- или 2.-)"],
        ["AI пропустил", "GT диалог не найден в AI отчёте"],
        ["Ложное срабатывание", "Комментарий: 'клиента не было'"],
        ["Неоднозначно", "Неясная классификация"],
        ["Недостаточно данных", "Нет человеческой разметки"],
        ["", ""],
        ["ОБРАЗЕЦ СТРУКТУРЫ", ""],
        ["Файл-образец", "regression_0903/TIMELINE_GT_AI.xlsx"],
        ["", ""],
        ["СОЗДАНО", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
    ]
    
    for row_idx, row_data in enumerate(methodology_text, 1):
        methodology_sheet.cell(row=row_idx, column=1, value=row_data[0])
        if len(row_data) > 1:
            methodology_sheet.cell(row=row_idx, column=2, value=row_data[1])
    
    methodology_sheet.column_dimensions["A"].width = 30
    methodology_sheet.column_dimensions["B"].width = 60
    
    # Save workbook
    if OUTPUT_FILE.exists():
        backup_file = OUTPUT_FILE.parent / f"TIMELINE_GT_AI_2026-10-02_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        shutil.copy2(OUTPUT_FILE, backup_file)
        print(f"Backup created: {backup_file}")
    
    wb.save(OUTPUT_FILE)
    print(f"Report saved: {OUTPUT_FILE}")
    
    return {
        "included_stores": included_stores,
        "excluded_stores": excluded_stores,
        "store_data": store_data
    }


def main():
    """Main entry point."""
    print("Loading data...")
    df_final, comments, manual_dialogs = load_data()
    
    print(f"  AI dialogs: {len(df_final)}")
    print(f"  GT comments: {len(comments)}")
    print(f"  GT manual dialogs: {len(manual_dialogs)}")
    
    print("\nCreating Excel report...")
    result = create_excel_report(df_final, comments, manual_dialogs)
    
    print("\n" + "=" * 70)
    print("РЕЗУЛЬТАТ")
    print("=" * 70)
    print(f"\nФайл: {OUTPUT_FILE}")
    print(f"\nВключено магазинов: {len(result['included_stores'])}")
    for store in result['included_stores']:
        stats = result['store_data'][store]['stats']
        print(f"  - {store}:")
        print(f"      GT={stats['total_gt']}, AI={stats['total_ai']}")
        print(f"      Совпало={stats['correct']}, Ошибок={stats['error']}, "
              f"Пропущено={stats['missed']}, Ложных={stats['false_positive']}, "
              f"Неоднозначно={stats['ambiguous']}, Недостаточно={stats['insufficient']}")
    
    print(f"\nИсключено магазинов: {len(result['excluded_stores'])}")
    for store, reason in result['excluded_stores']:
        print(f"  - {store}: {reason}")
    
    # Summary stats
    total_gt = sum(sd["stats"]["total_gt"] for sd in result['store_data'].values())
    total_ai = sum(sd["stats"]["total_ai"] for sd in result['store_data'].values())
    total_correct = sum(sd["stats"]["correct"] for sd in result['store_data'].values())
    total_error = sum(sd["stats"]["error"] for sd in result['store_data'].values())
    total_missed = sum(sd["stats"]["missed"] for sd in result['store_data'].values())
    total_fp = sum(sd["stats"]["false_positive"] for sd in result['store_data'].values())
    total_ambiguous = sum(sd["stats"]["ambiguous"] for sd in result['store_data'].values())
    total_insufficient = sum(sd["stats"]["insufficient"] for sd in result['store_data'].values())
    
    print(f"\n{'=' * 70}")
    print("ОБЩАЯ СТАТИСТИКА")
    print("=" * 70)
    print(f"  GT записей: {total_gt}")
    print(f"  AI диалогов: {total_ai}")
    print(f"  Совпадений: {total_correct}")
    print(f"  Ошибок AI: {total_error}")
    print(f"  Пропусков AI: {total_missed}")
    print(f"  Ложных срабатываний: {total_fp}")
    print(f"  Неоднозначно: {total_ambiguous}")
    print(f"  Недостаточно данных: {total_insufficient}")


if __name__ == "__main__":
    main()
