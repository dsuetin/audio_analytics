#!/usr/bin/env python3
"""
Генерация Excel-отчёта AI vs GT.

Использует финальный отчёт AI-классификации и комментарии экспертов
для создания упрощённого отчёта с 4 колонками.

Использование:
    python3 generate_report.py --date 2026-10-02

Параметры:
    --date          Дата в формате YYYY-MM-DD (обязательный)
    --final-report  Путь к финальному отчёту AI
    --comments      Путь к файлу комментариев
    --output        Путь к выходному файлу
    --no-backup     Не создавать резервную копию
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill


# Константы
BASE_DIR = Path(__file__).resolve().parents[2]  # skills/generate_ai_vs_gt_report -> audio_analytics
REPORTS_DIR = BASE_DIR / "reports"


def parse_time_to_minutes(time_str: Optional[str]) -> tuple[Optional[float], Optional[str]]:
    """
    Convert time string to (minutes_from_start, formatted_time).
    
    Supported formats:
    - '2026-10-02 08:38:28.581000' (AI format with date and microseconds)
    - '2026-10-02 08:38:28' (AI format with date)
    - '08:38:28' (AI format without date)
    - '11:10:35' (GT format)
    - '08:38' (short format)
    
    Returns:
        (minutes_from_start, formatted_time_as_HH:MM:SS) or (None, None) if parsing fails
    """
    if not time_str:
        return None, None
    
    time_str = str(time_str).strip()
    
    try:
        # Extract time part (remove date if present)
        if '-' in time_str and len(time_str.split('-')) == 3:
            # Format: 'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DD HH:MM'
            time_part = time_str.split(' ')[1] if ' ' in time_str else time_str
        else:
            # Format: 'HH:MM:SS' or 'HH:MM'
            time_part = time_str
        
        # Parse hours, minutes, seconds (handle microseconds)
        parts = time_part.split(':')
        if len(parts) < 2:
            return None, None
        
        hours = int(parts[0])
        minutes = int(parts[1])
        
        # Handle seconds (may contain microseconds like '28.581000')
        if len(parts) > 2:
            seconds_part = parts[2].split('.')[0]  # Remove microseconds
            seconds = int(seconds_part)
        else:
            seconds = 0
        
        # Validate time values
        if not (0 <= hours <= 23 and 0 <= minutes <= 59 and 0 <= seconds <= 59):
            return None, None
        
        # Calculate minutes from start of day
        time_minutes = hours * 60 + minutes + seconds / 60
        
        # Format as HH:MM:SS (always show seconds for consistency)
        formatted_time = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        
        return time_minutes, formatted_time
    
    except (ValueError, IndexError):
        return None, None


def is_positive_confirmation(comment: Optional[str]) -> bool:
    """
    Check if comment is a positive confirmation (1.+ 2.+).
    
    Recognizes various formats:
    - "1. + 2. +"
    - "1.+ 2.+"
    - "1.+ 2. +"
    - "1. + 2.+"
    - "1+2+"
    """
    if not comment:
        return False
    
    comment_lower = str(comment).lower().strip()
    
    # Normalize spaces for comparison
    normalized = comment_lower.replace(' ', '')
    
    # Check for positive confirmation patterns
    positive_patterns = ['1+2+', '1.+2.+']
    
    return any(pattern in normalized for pattern in positive_patterns)


def load_final_report(final_report_path: Path) -> pd.DataFrame:
    """Load final transcript report from Excel."""
    if not final_report_path.exists():
        raise FileNotFoundError(f"Final report not found: {final_report_path}")
    
    df = pd.read_excel(final_report_path)
    return df


def load_comments(comments_path: Path, date: str) -> dict:
    """Load and filter comments by date."""
    if not comments_path.exists():
        raise FileNotFoundError(f"Comments file not found: {comments_path}")
    
    with open(comments_path) as f:
        all_comments = json.load(f)
    
    # Filter by date
    filtered_comments = {}
    for key, value in all_comments.items():
        if date in key:
            filtered_comments[key] = value
    
    return filtered_comments


def load_manual_dialogs(manual_dialogs_path: Path, date: str) -> list:
    """Load manual dialogs filtered by date."""
    if not manual_dialogs_path.exists():
        return []
    
    with open(manual_dialogs_path) as f:
        all_manual = json.load(f)
    
    # Filter by date
    filtered_manual = [m for m in all_manual if m.get("date") == date]
    
    return filtered_manual


def normalize_store_name(store_name: str) -> str:
    """
    Normalize store name by grouping variations.
    
    Known variations:
    - "37" vs "378" (Армавир Советской Армии)
    - "_" vs "_37" (Кисловодск 40 лет Октября)
    - "строен" vs "строение_1" (Пятигорск Мира 201а)
    - "_" vs "_А" (Ставрополь Доваторцев 62)
    - "(УТ" vs "(УТЭП" (Иноземцево Гагарина 2Е)
    
    Returns normalized store name (the main/longer version).
    """
    if not store_name:
        return store_name
    
    # Define store name variations and their normalized form
    variations = {
        # Армавир Советской Армии
        "г_Армавир_ул_Советской_Армии_37": "г_Армавир_ул_Советской_Армии_37",
        "г_Армавир_ул_Советской_Армии_378": "г_Армавир_ул_Советской_Армии_37",
        
        # Кисловодск 40 лет Октября
        "г_Кисловодск_ул_40_лет_Октября_": "г_Кисловодск_ул_40_лет_Октября_",
        "г_Кисловодск_ул_40_лет_Октября_37": "г_Кисловодск_ул_40_лет_Октября_",
        
        # Пятигорск Мира 201а
        "г_Пятигорск_ул_Мира_201а_строен": "г_Пятигорск_ул_Мира_201а_строен",
        "г_Пятигорск_ул_Мира_201а_строение_1": "г_Пятигорск_ул_Мира_201а_строен",
        
        # Ставрополь Доваторцев 62
        "г_Ставрополь_ул._Доваторцев_62_": "г_Ставрополь_ул._Доваторцев_62_",
        "г_Ставрополь_ул._Доваторцев_62_А": "г_Ставрополь_ул._Доваторцев_62_",
        
        # Иноземцево Гагарина 2Е (note: manual dialogs have closing parenthesis)
        "п_Иноземцево_ул_Гагарина_2Е_(УТ": "п_Иноземцево_ул_Гагарина_2Е_(УТ",
        "п_Иноземцево_ул_Гагарина_2Е_(УТЭП": "п_Иноземцево_ул_Гагарина_2Е_(УТ",
        "п_Иноземцево_ул_Гагарина_2Е_(УТЭП)": "п_Иноземцево_ул_Гагарина_2Е_(УТ",
    }
    
    return variations.get(store_name, store_name)


def get_stores_with_human_annotation(comments: dict, manual_dialogs: list, date: str) -> tuple[set, dict]:
    """
    Get set of stores that have human annotation (comments or manual dialogs).
    
    Returns:
        tuple: (set of normalized store IDs, dict mapping normalized store to all its variations)
    """
    store_variations = {}  # normalized_store -> set of all variations
    
    # Extract stores from comments
    for key in comments.keys():
        if date not in key:
            continue
        
        # Extract store name from key (format: YYYY-MM-DD_store_client_X)
        parts = key.split("_")
        if len(parts) < 3:
            continue
        
        store_parts = []
        for part in parts[1:]:
            if part in ["client", "Manual"]:
                break
            store_parts.append(part)
        
        store_name = "_".join(store_parts)
        normalized_store = normalize_store_name(store_name)
        
        if normalized_store:
            if normalized_store not in store_variations:
                store_variations[normalized_store] = set()
            store_variations[normalized_store].add(store_name)
    
    # Extract stores from manual dialogs
    for md in manual_dialogs:
        if md.get("date") != date:
            continue
        
        store_name = md.get("store_id", "")
        if store_name:
            normalized_store = normalize_store_name(store_name)
            if normalized_store:
                if normalized_store not in store_variations:
                    store_variations[normalized_store] = set()
                store_variations[normalized_store].add(store_name)
    
    # Exclude test_store from final list
    stores_with_annotations = {s for s in store_variations.keys() if s != "test_store"}
    
    return stores_with_annotations, store_variations


def extract_client_id_from_key(key: str) -> Optional[str]:
    """Extract client_id from comment key (format: YYYY-MM-DD_store_client_X)."""
    parts = key.split("_")
    
    try:
        client_idx = parts.index("client")
        if client_idx + 1 < len(parts):
            return f"client_{parts[client_idx + 1]}"
    except ValueError:
        pass
    
    return None


def match_comments_to_ai_dialogs(df: pd.DataFrame, comments: dict, store_id: str, store_variations: dict) -> dict:
    """
    Match comments to AI dialogs for a specific store.
    
    Handles store name variations (e.g., "37" vs "378") using store_variations mapping.
    """
    store_comments = {}
    
    # Get all variations of this store name
    normalized_store = normalize_store_name(store_id)
    variations = store_variations.get(normalized_store, {store_id})
    
    for key, comment in comments.items():
        # Check if this comment belongs to any variation of the store
        for variation in variations:
            if variation in key:
                client_id = extract_client_id_from_key(key)
                if client_id:
                    store_comments[client_id] = comment
                break
    
    return store_comments


def generate_report_for_store(store_id: str, ai_dialogs: list, comments: dict, manual_dialogs: list = None) -> list:
    """Generate report rows for a single store.
    
    Includes both AI dialogs and manual dialogs (GT dialogs missed by AI).
    """
    rows = []
    
    # Process AI dialogs
    for ai_dialog in ai_dialogs:
        ai_id = ai_dialog.get("client_id", "")
        
        # Get time
        dialog_start = ai_dialog.get("dialog_start_at")
        time_minutes, formatted_time = parse_time_to_minutes(str(dialog_start) if dialog_start else None)
        
        if formatted_time is None:
            continue  # Skip rows without valid time
        
        # Get AI classification
        ai_mission = ai_dialog.get("dialog_type", "")
        ai_outcome = "Покупка" if ai_dialog.get("is_sale") else "Нет покупки"
        
        # Get comment
        comment = comments.get(ai_id, "")
        
        # Check for positive confirmation
        if is_positive_confirmation(comment):
            comment = "✔"
        
        rows.append({
            'time_minutes': time_minutes,
            'time_val': formatted_time,
            'ai_mission': ai_mission,
            'ai_outcome': ai_outcome,
            'comment': comment,
            'is_checkmark': comment == "✔"
        })
    
    # Process manual dialogs (GT dialogs missed by AI)
    if manual_dialogs:
        for md in manual_dialogs:
            # Get time
            start_time = md.get("start_time", "")
            time_minutes, formatted_time = parse_time_to_minutes(str(start_time) if start_time else None)
            
            if formatted_time is None:
                continue  # Skip rows without valid time
            
            # Get manual dialog info
            mission = md.get("dialog_type", "")
            result = md.get("result", "")
            outcome = "Покупка" if result == "buy" else "Нет покупки" if result == "no_buy" else result
            
            # Get comment (manual dialogs may have comments with "Manual" in key)
            comment = md.get("comment", "Ручной диалог")
            
            # Check for positive confirmation
            if is_positive_confirmation(comment):
                comment = "✔"
            
            rows.append({
                'time_minutes': time_minutes,
                'time_val': formatted_time,
                'ai_mission': mission,
                'ai_outcome': outcome,
                'comment': comment,
                'is_checkmark': comment == "✔"
            })
    
    # Sort by time
    rows.sort(key=lambda x: x['time_minutes'] if x['time_minutes'] is not None else 999999)
    
    return rows


def create_excel_report(stores_data: dict, output_path: Path, date: str):
    """Create Excel workbook with all store sheets."""
    wb = Workbook()
    
    # Remove default sheet
    default_sheet = wb.active
    wb.remove(default_sheet)
    
    total_rows = 0
    total_checkmarks = 0
    
    # Create sheet for each store
    for store_id, rows in stores_data.items():
        ws = wb.create_sheet(store_id[:31])  # Excel limit: 31 chars
        
        # Write header
        headers = ["Время", "AI — миссия", "AI — результат", "Комментарий"]
        for col_idx, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_idx, value=header)
            cell.font = Font(bold=True)
            cell.fill = PatternFill(start_color="DDDDDD", fill_type="solid")
        
        # Write data rows
        checkmarks_count = 0
        for row_idx, row_data in enumerate(rows, 2):
            ws.cell(row=row_idx, column=1, value=row_data['time_val'])
            ws.cell(row=row_idx, column=2, value=row_data['ai_mission'])
            ws.cell(row=row_idx, column=3, value=row_data['ai_outcome'])
            
            # Write comment with special formatting for checkmark
            comment_cell = ws.cell(row=row_idx, column=4, value=row_data['comment'])
            if row_data['is_checkmark']:
                comment_cell.font = Font(bold=True, size=14)
                comment_cell.alignment = Alignment(horizontal="center")
                checkmarks_count += 1
            else:
                comment_cell.alignment = Alignment(wrap_text=True)
        
        # Add totals at bottom
        totals_row = len(rows) + 3
        totals_cell = ws.cell(row=totals_row, column=1, value="ИТОГО:")
        totals_cell.font = Font(bold=True)
        ws.cell(row=totals_row, column=2, value=f"Всего диалогов: {len(rows)}")
        ws.cell(row=totals_row, column=3, value=f"С галочкой: {checkmarks_count}")
        ws.cell(row=totals_row, column=4, value=f"Соотношение: {len(rows)} / {checkmarks_count}")
        
        # Set column widths
        ws.column_dimensions["A"].width = 12
        ws.column_dimensions["B"].width = 25
        ws.column_dimensions["C"].width = 20
        ws.column_dimensions["D"].width = 30
        
        total_rows += len(rows)
        total_checkmarks += checkmarks_count
    
    # Create summary sheet
    summary_sheet = wb.create_sheet("Сводка")
    summary_data = [
        ["Дата отчёта:", date],
        ["Количество магазинов:", len(stores_data)],
        ["Всего диалогов:", total_rows],
        ["Всего галочек:", total_checkmarks],
        ["Дата генерации:", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
    ]
    
    for row_idx, row_data in enumerate(summary_data, 1):
        summary_sheet.cell(row=row_idx, column=1, value=row_data[0])
        if len(row_data) > 1:
            summary_sheet.cell(row=row_idx, column=2, value=row_data[1])
    
    summary_sheet.column_dimensions["A"].width = 30
    summary_sheet.column_dimensions["B"].width = 50
    
    # Save workbook
    wb.save(output_path)
    
    return {
        'stores': len(stores_data),
        'total_rows': total_rows,
        'total_checkmarks': total_checkmarks
    }


def validate_report(output_path: Path) -> dict:
    """Validate generated report."""
    from openpyxl import load_workbook
    
    wb = load_workbook(output_path)
    
    validation_results = {
        'valid': True,
        'errors': [],
        'warnings': []
    }
    
    stores_count = 0
    total_rows = 0
    
    for sheet_name in wb.sheetnames:
        if sheet_name == "Сводка":
            continue
        
        stores_count += 1
        ws = wb[sheet_name]
        
        # Check column count
        if ws.max_column != 4:
            validation_results['errors'].append(
                f"Лист '{sheet_name}': ожидаются 4 колонки, найдено {ws.max_column}"
            )
            validation_results['valid'] = False
        
        # Check header
        headers = [ws.cell(row=1, column=col).value for col in range(1, 5)]
        expected_headers = ["Время", "AI — миссия", "AI — результат", "Комментарий"]
        if headers != expected_headers:
            validation_results['warnings'].append(
                f"Лист '{sheet_name}': заголовки не совпадают"
            )
        
        # Check time format
        for row in range(2, min(ws.max_row + 1, 10)):
            time_val = ws.cell(row=row, column=1).value
            if time_val and len(str(time_val).split(':')) != 3:
                validation_results['warnings'].append(
                    f"Лист '{sheet_name}', строка {row}: время не в формате ЧЧ:ММ:СС"
                )
                break
        
        # Check totals row exists
        has_totals = False
        for row in range(ws.max_row - 2, ws.max_row + 1):
            if ws.cell(row=row, column=1).value == "ИТОГО:":
                has_totals = True
                break
        
        if not has_totals:
            validation_results['warnings'].append(
                f"Лист '{sheet_name}': не найдена итоговая строка"
            )
        
        total_rows += ws.max_row - 1
    
    if stores_count == 0:
        validation_results['errors'].append("Нет листов с данными магазинов")
        validation_results['valid'] = False
    
    validation_results['stores_count'] = stores_count
    validation_results['total_rows'] = total_rows
    
    return validation_results


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Генерация Excel-отчёта AI vs GT",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
    python3 generate_report.py --date 2026-10-02
    python3 generate_report.py --date 2026-10-02 --output custom_report.xlsx
        """
    )
    
    parser.add_argument(
        '--date',
        required=True,
        help='Дата в формате YYYY-MM-DD'
    )
    
    parser.add_argument(
        '--final-report',
        help='Путь к финальному отчёту AI'
    )
    
    parser.add_argument(
        '--comments',
        help='Путь к файлу комментариев'
    )
    
    parser.add_argument(
        '--manual-dialogs',
        help='Путь к файлу ручных диалогов'
    )
    
    parser.add_argument(
        '--output',
        help='Путь к выходному файлу'
    )
    
    parser.add_argument(
        '--no-backup',
        action='store_true',
        help='Не создавать резервную копию'
    )
    
    args = parser.parse_args()
    
    # Set default paths
    final_report_path = (
        Path(args.final_report) if args.final_report
        else REPORTS_DIR / f"final_transcript_report_{args.date}.xlsx"
    )
    
    comments_path = (
        Path(args.comments) if args.comments
        else REPORTS_DIR / "comments.json"
    )
    
    manual_dialogs_path = (
        Path(args.manual_dialogs) if args.manual_dialogs
        else REPORTS_DIR / "manual_dialogs.json"
    )
    
    output_path = (
        Path(args.output) if args.output
        else REPORTS_DIR / f"AI_vs_GT_{args.date}.xlsx"
    )
    
    print("=" * 80)
    print("ГЕНЕРАЦИЯ ОТЧЁТА AI vs GT")
    print("=" * 80)
    print(f"\nДата: {args.date}")
    print(f"Финальный отчёт: {final_report_path}")
    print(f"Комментарии: {comments_path}")
    print(f"Ручные диалоги: {manual_dialogs_path}")
    print(f"Выходной файл: {output_path}")
    
    # Create backup if needed
    if output_path.exists() and not args.no_backup:
        backup_path = output_path.parent / f"{output_path.stem}_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}{output_path.suffix}"
        shutil.copy2(output_path, backup_path)
        print(f"\nРезервная копия: {backup_path}")
    
    # Load data
    print("\nЗагрузка данных...")
    try:
        df = load_final_report(final_report_path)
        print(f"  AI диалоги: {len(df)}")
    except FileNotFoundError as e:
        print(f"  Ошибка: {e}")
        sys.exit(1)
    
    try:
        comments = load_comments(comments_path, args.date)
        print(f"  Комментарии: {len(comments)}")
    except FileNotFoundError as e:
        print(f"  Предупреждение: {e}")
        comments = {}
    
    manual_dialogs = load_manual_dialogs(manual_dialogs_path, args.date)
    print(f"  Ручные диалоги: {len(manual_dialogs)}")
    
    # Get stores with human annotation (CRITICAL: use GT as source, not AI report)
    print("\nОпределение магазинов с человеческой разметкой...")
    stores_with_gt, store_variations = get_stores_with_human_annotation(comments, manual_dialogs, args.date)
    
    print(f"  Магазинов с GT: {len(stores_with_gt)}")
    for store in sorted(stores_with_gt):
        variations = store_variations.get(store, {store})
        if len(variations) > 1:
            print(f"    - {store} (вариации: {', '.join(sorted(variations))})")
        else:
            print(f"    - {store}")
    
    # Check for stores in AI report but not in GT
    ai_stores = set(df["store_id"].unique())
    gt_stores_normalized = set()
    for store in ai_stores:
        gt_stores_normalized.add(normalize_store_name(store))
    
    stores_in_ai_not_in_gt = gt_stores_normalized - stores_with_gt
    if stores_in_ai_not_in_gt:
        print(f"\n  ⚠ Магазины в AI отчёте, но без GT:")
        for store in sorted(stores_in_ai_not_in_gt):
            print(f"    - {store}")
    
    # Check for stores in GT but not in AI
    stores_in_gt_not_in_ai = stores_with_gt - gt_stores_normalized
    if stores_in_gt_not_in_ai:
        print(f"\n  ⚠ Магазины в GT, но не в AI отчёте:")
        for store in sorted(stores_in_gt_not_in_ai):
            print(f"    - {store}")
    
    # Group by store (only stores with human annotation)
    print("\nГруппировка по магазинам...")
    stores_data = {}
    unmatched_gt_stores = []
    
    for normalized_store_id in stores_with_gt:
        # Get all variations of this store
        variations = store_variations.get(normalized_store_id, {normalized_store_id})
        
        # Collect AI dialogs from all variations
        ai_dialogs = []
        for variation in variations:
            variation_dialogs = df[df["store_id"] == variation].to_dict("records")
            ai_dialogs.extend(variation_dialogs)
        
        # Collect manual dialogs for all variations
        store_manual_dialogs = []
        for md in manual_dialogs:
            md_store = md.get("store_id", "")
            normalized_md_store = normalize_store_name(md_store)
            if normalized_md_store == normalized_store_id:
                store_manual_dialogs.append(md)
        
        # Match comments for all variations
        store_comments = match_comments_to_ai_dialogs(df, comments, normalized_store_id, store_variations)
        
        # Generate report rows (includes both AI and manual dialogs)
        rows = generate_report_for_store(normalized_store_id, ai_dialogs, store_comments, store_manual_dialogs)
        
        if rows:
            stores_data[normalized_store_id] = rows
            print(f"  {normalized_store_id}: {len(rows)} записей (AI: {len(ai_dialogs)}, Manual: {len(store_manual_dialogs)})")
        else:
            # Store has GT annotation but no matching AI dialogs
            unmatched_gt_stores.append((normalized_store_id, len(store_comments), len(ai_dialogs), len(store_manual_dialogs)))
            print(f"  {normalized_store_id}: ⚠ Нет AI диалогов (GT комментариев: {len(store_comments)}, Manual: {len(store_manual_dialogs)})")
    
    # Report unmatched stores
    if unmatched_gt_stores:
        print(f"\n  ⚠ Магазины с GT, но без сопоставимых AI диалогов:")
        for store, comment_count, ai_count in unmatched_gt_stores:
            print(f"    - {store}: {comment_count} комментариев, {ai_count} AI диалогов")
    
    # Generate Excel
    print(f"\nГенерация Excel (магазинов: {len(stores_data)})...")
    stats = create_excel_report(stores_data, output_path, args.date)
    
    print(f"\nРезультат:")
    print(f"  Магазинов: {stats['stores']}")
    print(f"  Всего диалогов: {stats['total_rows']}")
    print(f"  Всего галочек: {stats['total_checkmarks']}")
    
    # Validate
    print("\nВалидация отчёта...")
    validation = validate_report(output_path)
    
    if validation['valid']:
        print("  ✓ Отчёт валиден")
    else:
        print("  ✗ Ошибки валидации:")
        for error in validation['errors']:
            print(f"    - {error}")
    
    if validation['warnings']:
        print("  ⚠ Предупреждения:")
        for warning in validation['warnings']:
            print(f"    - {warning}")
    
    print(f"\n{'=' * 80}")
    print(f"Отчёт сохранён: {output_path.absolute()}")
    print(f"{'=' * 80}")


if __name__ == "__main__":
    main()
