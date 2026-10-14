#!/usr/bin/env python3
"""
Проверка наличия текста в БД для ручных диалогов за 02.10.2026

Определяет, являются ли ручные диалоги:
1. Пропущенными системой (текст есть, но диалог не сформирован)
2. Проблемой аудио/ASR (текста нет в БД)
"""

import json
import asyncpg
import os
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

DATE = "2026-10-02"
REPORTS_DIR = Path("/home/developer/devbox/audio_analytics/reports")

async def main():
    # Load manual dialogs
    with open(REPORTS_DIR / "manual_dialogs.json") as f:
        manual_dialogs = json.load(f)
    
    manual_10_02 = [d for d in manual_dialogs if d.get("date") == DATE]
    
    print(f"Всего ручных диалогов за {DATE}: {len(manual_10_02)}")
    
    # Connect to PostgreSQL
    conn = await asyncpg.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "speech"),
        password=os.getenv("POSTGRES_PASSWORD", "speech"),
        database=os.getenv("POSTGRES_DB", "speech_db"),
    )
    
    results = {
        "has_text": [],
        "no_text": [],
        "error": []
    }
    
    for md in manual_10_02:
        store_id = md.get("store_id")
        start_time = md.get("start_time")
        end_time = md.get("end_time")
        
        # Skip test store
        if store_id == "test_store":
            continue
        
        # Build datetime for query
        try:
            start_dt = datetime.strptime(f"{DATE}T{start_time}", "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            end_dt = datetime.strptime(f"{DATE}T{end_time}", "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            
            # Query transcripts for this interval
            transcripts = await conn.fetch("""
                SELECT session_id, recognition_text, created_at
                FROM transcripts
                WHERE store_id = $1
                AND created_at >= $2
                AND created_at <= $3
                ORDER BY created_at
            """, store_id, start_dt, end_dt)
            
            if transcripts:
                results["has_text"].append({
                    "store_id": store_id,
                    "start_time": start_time,
                    "end_time": end_time,
                    "dialog_type": md.get("dialog_type"),
                    "result": md.get("result"),
                    "transcript_count": len(transcripts),
                    "sample_text": transcripts[0]["recognition_text"][:100] if transcripts else None
                })
            else:
                results["no_text"].append({
                    "store_id": store_id,
                    "start_time": start_time,
                    "end_time": end_time,
                    "dialog_type": md.get("dialog_type"),
                    "result": md.get("result")
                })
        
        except Exception as e:
            results["error"].append({
                "store_id": store_id,
                "start_time": start_time,
                "end_time": end_time,
                "error": str(e)
            })
    
    await conn.close()
    
    # Print results
    print("\n" + "=" * 80)
    print("РЕЗУЛЬТАТЫ ПРОВЕРКИ")
    print("=" * 80)
    
    print(f"\n1. Текст ЕСТЬ в БД (пропущены системой): {len(results['has_text'])}")
    for item in results["has_text"][:10]:  # Show first 10
        print(f"  {item['store_id']}: {item['start_time']} - {item['end_time']}")
        print(f"    Тип: {item['dialog_type']}, Итог: {item['result']}")
        print(f"    Записей: {item['transcript_count']}, Текст: {item['sample_text']}...")
    
    if len(results["has_text"]) > 10:
        print(f"  ... и ещё {len(results['has_text']) - 10}")
    
    print(f"\n2. Текста НЕТ в БД (проблема аудио/ASR): {len(results['no_text'])}")
    for item in results["no_text"][:10]:  # Show first 10
        print(f"  {item['store_id']}: {item['start_time']} - {item['end_time']}")
        print(f"    Тип: {item['dialog_type']}, Итог: {item['result']}")
    
    if len(results["no_text"]) > 10:
        print(f"  ... и ещё {len(results['no_text']) - 10}")
    
    print(f"\n3. Ошибки: {len(results['error'])}")
    for item in results["error"]:
        print(f"  {item['store_id']}: {item['error']}")
    
    # Summary
    print("\n" + "=" * 80)
    print("ВЫВОДЫ")
    print("=" * 80)
    
    if results["has_text"]:
        print(f"\n❗ {len(results['has_text'])} ручных диалогов имеют текст в БД")
        print("   → Это проблема СЕГМЕНТАЦИИ (система не сформировала диалог)")
        print("   → Требуется анализ: почему segmentation пропустила эти интервалы?")
    
    if results["no_text"]:
        print(f"\n❗ {len(results['no_text'])} ручных диалогов НЕ имеют текста в БД")
        print("   → Это проблема АУДИО/ASR (текст не распознан)")
        print("   → Требуется проверка: аудио файлы, VAD, ASR pipeline")
    
    # Save detailed results
    output_file = REPORTS_DIR / "manual_dialogs_text_check_2026-10-02.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    print(f"\n📄 Детальные результаты сохранены в: {output_file}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
