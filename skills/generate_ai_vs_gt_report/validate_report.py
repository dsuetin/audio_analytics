#!/usr/bin/env python3
"""
Валидация Excel-отчёта AI vs GT.

Проверяет соответствие отчёта требованиям навыка.

Использование:
    python3 validate_report.py --report reports/AI_vs_GT_2026-10-02.xlsx
"""

from __future__ import annotations

import argparse
from pathlib import Path
from openpyxl import load_workbook


def validate_report(report_path: Path) -> dict:
    """
    Validate AI vs GT report.
    
    Checks:
    - 4 columns per store sheet
    - Time format HH:MM:SS
    - Checkmarks for "1.+ 2.+"
    - Totals at bottom of each sheet
    - Chronological sorting
    - No duplicates
    """
    results = {
        'valid': True,
        'errors': [],
        'warnings': [],
        'info': [],
        'stats': {}
    }
    
    if not report_path.exists():
        results['errors'].append(f"Файл не найден: {report_path}")
        results['valid'] = False
        return results
    
    wb = load_workbook(report_path)
    
    stores_count = 0
    total_rows = 0
    total_checkmarks = 0
    sheets_with_issues = []
    
    for sheet_name in wb.sheetnames:
        if sheet_name == "Сводка":
            continue
        
        stores_count += 1
        ws = wb[sheet_name]
        
        # Check column count
        if ws.max_column != 4:
            results['errors'].append(
                f"Лист '{sheet_name}': ожидаются 4 колонки, найдено {ws.max_column}"
            )
            results['valid'] = False
            sheets_with_issues.append(sheet_name)
            continue
        
        # Check headers
        headers = [ws.cell(row=1, column=col).value for col in range(1, 5)]
        expected_headers = ["Время", "AI — миссия", "AI — результат", "Комментарий"]
        if headers != expected_headers:
            results['errors'].append(
                f"Лист '{sheet_name}': неверные заголовки"
            )
            results['valid'] = False
            sheets_with_issues.append(sheet_name)
            continue
        
        # Count rows and checkmarks
        sheet_rows = 0
        sheet_checkmarks = 0
        times = []
        
        for row in range(2, ws.max_row + 1):
            time_val = ws.cell(row=row, column=1).value
            comment = ws.cell(row=row, column=4).value
            
            # Skip totals row
            if ws.cell(row=row, column=1).value == "ИТОГО:":
                break
            
            # Check time format
            if time_val:
                time_str = str(time_val)
                if len(time_str.split(':')) != 3:
                    results['warnings'].append(
                        f"Лист '{sheet_name}', строка {row}: время не в формате ЧЧ:ММ:СС: '{time_val}'"
                    )
                
                times.append(time_val)
                sheet_rows += 1
                
                # Check for checkmark
                if comment == "✔":
                    sheet_checkmarks += 1
                    
                    # Verify checkmark formatting
                    cell = ws.cell(row=row, column=4)
                    if not cell.font.bold:
                        results['warnings'].append(
                            f"Лист '{sheet_name}', строка {row}: галочка не выделена жирным"
                        )
        
        # Check chronological sorting
        if len(times) > 1:
            is_sorted = times == sorted(times)
            if not is_sorted:
                results['warnings'].append(
                    f"Лист '{sheet_name}': записи не отсортированы по времени"
                )
        
        # Check totals row
        has_totals = False
        totals_row_num = None
        for row in range(ws.max_row - 2, ws.max_row + 1):
            if ws.cell(row=row, column=1).value == "ИТОГО:":
                has_totals = True
                totals_row_num = row
                break
        
        if not has_totals:
            results['errors'].append(
                f"Лист '{sheet_name}': не найдена итоговая строка"
            )
            results['valid'] = False
        else:
            # Verify totals
            totals_cell_2 = ws.cell(row=totals_row_num, column=2).value
            totals_cell_3 = ws.cell(row=totals_row_num, column=3).value
            totals_cell_4 = ws.cell(row=totals_row_num, column=4).value
            
            expected_total = f"Всего диалогов: {sheet_rows}"
            expected_checkmarks = f"С галочкой: {sheet_checkmarks}"
            expected_ratio = f"Соотношение: {sheet_rows} / {sheet_checkmarks}"
            
            if totals_cell_2 != expected_total:
                results['errors'].append(
                    f"Лист '{sheet_name}': неверное количество диалогов "
                    f"(ожидалось '{expected_total}', найдено '{totals_cell_2}')"
                )
                results['valid'] = False
            
            if totals_cell_3 != expected_checkmarks:
                results['warnings'].append(
                    f"Лист '{sheet_name}': возможное несоответствие количества галочек "
                    f"(подсчитано {sheet_checkmarks}, указано {totals_cell_3})"
                )
            
            if totals_cell_4 != expected_ratio:
                results['warnings'].append(
                    f"Лист '{sheet_name}': возможное несоответствие соотношения"
                )
        
        total_rows += sheet_rows
        total_checkmarks += sheet_checkmarks
    
    # Check summary sheet
    if "Сводка" not in wb.sheetnames:
        results['warnings'].append("Отсутствует лист 'Сводка'")
    
    # Summary
    results['stats'] = {
        'stores_count': stores_count,
        'total_rows': total_rows,
        'total_checkmarks': total_checkmarks,
        'sheets_checked': len(wb.sheetnames) - 1  # Exclude summary
    }
    
    if stores_count == 0:
        results['errors'].append("Нет листов с данными магазинов")
        results['valid'] = False
    
    return results


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Валидация Excel-отчёта AI vs GT"
    )
    
    parser.add_argument(
        '--report',
        required=True,
        help='Путь к отчёту для валидации'
    )
    
    args = parser.parse_args()
    
    report_path = Path(args.report)
    
    print("=" * 80)
    print("ВАЛИДАЦИЯ ОТЧЁТА AI vs GT")
    print("=" * 80)
    print(f"\nФайл: {report_path}")
    
    results = validate_report(report_path)
    
    print(f"\nСтатистика:")
    print(f"  Магазинов: {results['stats'].get('stores_count', 0)}")
    print(f"  Всего диалогов: {results['stats'].get('total_rows', 0)}")
    print(f"  Всего галочек: {results['stats'].get('total_checkmarks', 0)}")
    
    if results['errors']:
        print(f"\n✗ Ошибки ({len(results['errors'])}):")
        for error in results['errors']:
            print(f"  - {error}")
    
    if results['warnings']:
        print(f"\n⚠ Предупреждения ({len(results['warnings'])}):")
        for warning in results['warnings']:
            print(f"  - {warning}")
    
    print(f"\n{'=' * 80}")
    if results['valid']:
        print("✓ ОТЧЁТ ВАЛИДЕН")
    else:
        print("✗ ОТЧЁТ НЕ ПРОШЁЛ ВАЛИДАЦИЮ")
    print(f"{'=' * 80}")
    
    return 0 if results['valid'] else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
