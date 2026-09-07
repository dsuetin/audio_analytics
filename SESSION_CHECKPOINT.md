# HANDOFF CHECKPOINT — AUDIO ANALYTICS (offline analysis)

> **Для следующего сессии (OpenCode/агент).**
> Чейкпоинт содержит:
>   1. Состояние системы на момент передачи (13 дат backfilled, ALL VALID).
>   2. Подтверждённые P0-проблемы (смешение клиентов) с данными.
>   3. **Точные места в коде**, которые нужно менять + обоснование.
>   4. Правила безопасности (что нельзя сломать).
>   5. Команды запуска и валидации.
>   6. **Порядок работы** для следующей сессии.
>
> Последнее обновление: 2026-09-04 (конец сессии #1).

---

## 1. Состояние на момент передачи

- Все 13 дат `reports/transcript_report_*.xlsx` → `final_transcript_report_<d>.xlsx + .pdf + _debug.json`
  (через `offline_analysis/run.py`, LLM `qwen3.8:27b`, ctx 131072 / predict 128000).
- **Валидация**: `validate_finals.py` → ALL FINAL VALID; PDF через `pdfinfo` → ALL PDF VALID.
- **Покрытие** (все 13 дат): `coverage.covered_rows == total`, `unassigned=0`, `duplicated=0`,
  `llm_errors=0`. Техническая валидность подтверждена.
- **GT (ground truth) baseline 08-27** (режим `--sales-ground-truth`):
  80 реальных продаж; strict recall **0.325**, recall+ambiguous **0.4375**, precision **0.946**;
  39 NOT_FOUND, 15 false-positive buy, 10 multi-sale dialogs.
  → Файл: `reports/_experiment_baseline_08-27.json`.
- Бэкап старых finals: `/tmp/opencode/final_backup_20260904_161738/`
  (repo-дир `reports_root_backup/` root-owned и не пишется user `developer`; `sudo` без пароля нет).
- **Ничего в исходном коде/промптах/thresholds НЕ менялось** (требование сессии #1).

## 2. Подтверждённые P0-проблемы (с доказательствами)

### 2.1. Смешение нескольких клиентов в ОДИН final-диалог — ГЛАВНАЯ

Подтверждено анализом raw-транскрипции внутри длиннейших `final` dialog-сегментов
(инструмент: `/tmp/opencode/long_dialog.py`).

- **08-27 client_3** (buy, is_sale=True, dur **27.9 мин / 1672 с**)
  - span 09:24 – 09:52, store `г_Пятигорск_ул_Первомайская_д_3`, 220 raw rows.
  - **3 независимых "добрый день"** внутри span → 2–3 разных клиента.
  - **НЕ попал в ре-сплит**: `dur (1672s) < REVIEW_LONG_SEC (1800s=30min)` → threshold-gate.
- **08-25 client_11** (buy, is_sale=True, dur **23.4 мин / 1406 с**)
  - **2 "добрый вечер/добрый"** внутри span → вероятно 2 клиента.
  - **НЕ попал в ре-сплит**: `dur (1406s) < 1800`.

### 2.2. Кандидаты, где re-сплит НЕ разрезал (LLM returned `[]` = «оставь как есть»)

- **08-29** client_3 (buy/True, **58.7 мин / 3523 s**): `kept_single` — `last_error=None`
  → LLM честно вернула `[]` ("один клиент"). По тексту это **один клиент** (длинный выбор АКБ +
  установка). Т.е. re-сплит **сработал верно** — не надо было резать. **ОК.**
- **09-03** (3 кандидата): `segment_012 1819s`, `segment_015 2537s`, `segment_022 1949s` — все
  `kept_single` (`last_error=None`) → LLM вернула `[]`. **Требуют визуального разбора** (см. §4.3).

### 2.3. Деградация сегментации (пустой ответ LLM → одиночные `unknown`)

| дата | unknown segments | доля |
|------|------------------|------|
| 08-25 | 885 | 91% |
| 08-27 | 1117 | 88% |
| 08-28 | 2374 | 97% |
| 08-29 | 1067 | 92% |
| 08-30 | 1275 | 98% |

Признак: `llm_error` у одиночных `unknown`-сегментов — в debug не всегда виден напрямую, но `repair_segments`
в `offline_analysis/segmentation.py:324` делает fallback в `unknown` при невалидном ответе.
Возможная причина: LLM не помещает ответ в `num_predict`, или chunk слишком большой → LLM молчит /
возвращает обрезанный JSON. **Влияет на recall** (покупка в «unknown»-чанке не станет `buy`).

### 2.4. `coverage.ok=false` при `covered==total` (lost=0) — **ЛОЖНЫЙ сарегон, P1-bug**

На 08-25/08-26/08-27/08-28/08-29/08-30/09-02/09-03: `coverage.ok = False`, при этом
`coverage.covered_rows == coverage.total_rows == total_input_rows`, **и** `sum(s.row_count) == total`.

**Подтверждено:** в `run.py:422-423` вычисляется
```
covered = [i for s in segments for i in range(s.start_index, s.end_index + 1)]
coverage_ok = (set(covered) == set(range(total_input_rows)) and len(covered) == total_input_rows)
```
но `Segment.start_index`/`end_index` — это **per-store** индексы (см.
`segmentation.py:182-191`, `index=i` внутри `enumerate(df.itertuples(index=False))`,
каждый магазин начинает с 0). В debug-сегментах на 08-27:

```
main_store                      min_start=0 max_end=  76   sum_row_count=  77
г_Минеральные_Воды_ул_Гагарина_ min_start=0 max_end=1770   sum_row_count=1771
г_Минеральные_Воды_ул_Московска min_start=0 max_end=3190   sum_row_count=3191
г_Пятигорск_ул_Калинина_д_299   min_start=0 max_end=2401   sum_row_count=2402
г_Пятигорск_ул_Первомайская_д_3 min_start=0 max_end= 890   sum_row_count= 891
г_Пятигорск_ш_Бештаугорское_д_4 min_start=0 max_end=1927   sum_row_count=1928
```

Сумма = 10260 (совпадает с `total_input_rows`), но **индексы каждого магазина
перекрываются** (все начинаются с 0). Покрытие union = 3191 < 10260 → `ok=False`.

**Вывод:** реальное покрытие полное (ни одной потерянной/дублированной строки).
**Не подменяет качество** — но **мешает диагностике** и может вводить в заблуждение.

**Решение (P1):**
- **Все индексы в системе — PER-STORE по дизайну** (`segmentation.py:182-191` `index=i`;
  инвариант `assert s.start_index == prev + 1; assert prev == len(rows)-1` — `segmentation.py:742-745`,
  срабатывает на строки ОДНОГО магазина). `sales_gt.py` матчит по **времени + store**, НЕ по индексам.
  Значит **ре-базировать в глобальное пространство НЕЛЬЗЯ** — сломает per-store инварианты и re-split.
- **Вариант A (ПРАВИЛЬНЫЙ и безопасный):** починить ТОЛЬКО метрику `coverage` в `run.py:422-423`:
  вместо пересечения индексов считать по **sum(row_count)**:
  ```
  covered_rows_sum = sum(getattr(s, 'row_count', (s.end_index - s.start_index + 1)) for s in segments)
  coverage_ok = (covered_rows_sum == total_input_rows)   # no loss + no dup, по сумме строк
  ```
  Это корректно: `sum(row_count) == total` — ни потерянных, ни дублей (проверено на всех 9 дат).
- **NE** менять индексы сегментов на глобальные (нарушит `resplit_long_segments`, `build_final_rows`).

## 3. Код: ТОЧНЫЕ МЕСТА ДЛЯ ИЗМЕНЕНИЙ

### 3.1. Порог re-сплита — ПРЯМАЯ ПРИЧИНА escaped P0
`offline_analysis/segmentation.py:503`
```
REVIEW_LONG_SEC = 30 * 60   # 1800 с — re-сплит ТОЛЬКО если dur >= 1800
VERY_LONG_SEC   = 60 * 60   # 3600 с (только для emphasis в prompt)
```
**Проблема:** 08-27 client_3 (1672s) и 08-25 client_11 (1406s) **никогда не попадают в re-сплит** —
они короче 1800s. Между тем именно они содержат **нескольких** клиентов (3×приветствие, 2×приветствие).
**Решение (кандидаты, по убыванию уверенности):**
1. **Опустить `REVIEW_LONG_SEC` до ~600 с (10 мин) или 900 с (15 мин)** — покрывает большинство
   «несколько клиентов в одном диалоге» без чрезмерного LLM-трафика.
2. **Добавить pre-flight эвристику** (до ре-сплита) в `resplit_long_segments(...)`:
   считать количество `GREET` (приветствие) + `END` (до свидания/чека/оплаты) маркеров внутри
   dialog-сегмента. Если `GREET >= 2` → **принудительно** включить в ре-сплит (даже если
   `dur < review_min`). Это mechanical rule, не LLM — детерминированный «триггер» для LLM-ревью.
3. **Изменить критерий входа в re-сплит**: вместо `dur >= review_min` использовать
   `dur >= 600 OR greet_count >= 2`.
**Конкретные строки для правки** (см. §7 diff):
- `segmentation.py:637-656` (условие входа в цикл re-сплита, `for seg in segments:`).
- Новая функция `_grep_for_client_marks(rows) -> (greet, end)` — считает паттерны
  `привет|здравств|добрый|добро пожал|до свидания|до встречи|спасибо|чек|оплат|терминал`.
- Новый параметр `force_review` в `REVIEW_LONG_SEC` (env `OFFLINE_RESPLIT_REVIEW_SEC`, default 1800).

### 3.2. Промпт re-сплита — **усиливать сигнал**, а не ослаблять порог
`offline_analysis/prompt.py:88-148` (`RESEGMENT_LONG_SYSTEM_PROMPT`)
- Сейчас prompt уже хорошо формулирует «2 приветствия = 2 клиента» (`prompt.py:124-125`).
- **Добавить в пользовательский prompt** (`build_long_dialog_resegment_prompt`,
  `prompt.py:151-190`) **счётчик маркеров** как факт: «в фрагменте N×приветствие,
  M×окончание (чек/оплата/прощание) — это ОБЯЗАТЕЛЬНО 2+ взаимодействия клиентов».
  LLM-модель склонна «игнорировать» маркеры, если их не выдать как data point.
- **Новый пункт в system prompt** (после `prompt.py:125`):
  ```
  • ОБЯЗАТЕЛЬНО: если в фрагменте 2+ независимых приветствия («здравствуйте», «добрый день»,
    «добрый» как начало обращения КЛИЕНТА) или 2+ завершённые оплаты/чеки — верни 2+ сегмента.
    Не верни segments:[], если таких маркеров больше одного.
  ```
**ОСТОРОЖНО:** не разрушить `segments:[]` на ОДИН действительно длительный разговор
(08-29 client_3, 58.7 мин, 1 клиент — сейчас корректно ОСТАВЛЕН). Баланс — через
count-based правило (пункт 3.1.2), а не через ослабление критерия в промпте.

### 3.3. Деградация / пустые ответы LLM (P1)
`offline_analysis/segmentation.py:875-910` (retry loop на чанке)
- Сейчас: 3 попытки, `last_err = ValueError("empty segmentation from LLM")`.
- **Добавить** (по убыванию эффективности):
  1. **Адаптивное уменьшение chunk** при 2+ неудачных попытках:
     `target_chunk_tokens = target_chunk_tokens * 0.6` для этого чанка (однажды, без повторной
     деградации). Это уже есть в `regression_new/lib.py:251` (ф-я `_segment_store_adaptive`)
     — **перенести в production** в `segmentation.py`.
  2. **Отдельный «fallback-классификатор»** на чанке: если LLM не вернул структуру — вернуть
     `unknown`-сегмент на каждый row (current), НО с флагом `degraded=True`, чтобы в debug
     было видно (и чтобы следующий run мог сделать adaptive-chunk).
  3. **Логировать** `resp.get("content", "")[:2000]` в debug (без PII: только length +
     hash + first 50 chars). Сейчас debug пишет только «empty segmentation» — не хватает,
     чтобы понять, обрезан ли ответ (hit `num_predict`) или LLM молчит.

### 3.4. `coverage.ok` — LOOSEN-БЕК (P1, корневая причина ПОДТВЕРЖДЕНА, см. §2.4)
Место: `offline_analysis/run.py:422-423`:
```
covered = [i for s in segments for i in range(s.start_index, s.end_index + 1)]
coverage_ok = (set(covered) == set(range(total_input_rows)) and len(covered) == total_input_rows)
```
**Причина (подтверждено):** `Segment.start_index`/`end_index` — **per-store** (`segmentation.py:182-191`,
каждый магазин от 0). Суммарно сравниваются с общим `total_input_rows` → у дат с >1 магазином
`set(covered)` перекрывается и `< total` → `ok=False`, **несмотря на полное реальное покрытие**
(`sum(row_count) == total` на всех 9 дат). На 09-01 (1 магазин) `ok=True`.

**Правка (вариант A — правильный, минимальный, безопасный):**
```
covered_rows_sum = sum(getattr(s, 'row_count', None) if getattr(s, 'row_count', None) is not None
                       else (s.end_index - s.start_index + 1) for s in segments)
coverage_ok = (covered_rows_sum == total_input_rows)
"covered_rows": covered_rows_sum,
```
- Это корректная метрика «нет потерь и нет дублей» через сумму строк (инвариант сохранён).
- **НЕ** менять индексы сегментов на глобальные — сломает per-store инвариант (`segmentation.py:742-745`)
  и `resplit_long_segments`.
- **НЕ** связывать с `unknown`-сегментами — это отдельная проблема (деградация LLM, §2.3).

### 3.5. LLM-бюджет (не менять, но учитывать)
`offline_analysis/segmentation.py:664-665` — `num_ctx=131072`, `num_predict=128000`.
`offline_analysis/llm.py` — `think=False` для thinking-модели.
`offline_analysis/run.py:74-75` — `PIPELINE_NUM_CTX/PREDICT`.
`offline_analysis/taxonomy.py:51` — `CONFIDENCE_MIN = 0.6`.
Всё это production-конфиг; менять — только после GT-прогона (см. §6).

## 4. Правила безопасности (НЕ ломать)

1. **GT-регрессия перед каждым изменением**:
   `python3 offline_analysis/run.py --sales-ground-truth "reports/Документы продаж за 27.08.2026г.xlsx" --input reports/transcript_report_2026-08-27.xlsx`
   Сравнить со `_experiment_baseline_08-27.json`. **Критерии:**
   - recall **не падает** (≥ 0.325);
   - precision **не падает** (≥ 0.946);
   - NOT_FOUND **не растёт** (≤ 39).
2. **Бэкап finals** перед перезаписью → `/tmp/opencode/` (root-owned `reports_root_backup/`
   не доступен user `developer`).
3. **Raw-файлы** `reports/transcript_report_*.xlsx` — **только чтение**.
4. **XLSX + PDF** строятся из **одних** `final_rows`. PDF без session_ids/client_id/CoT.
5. **Production-поведение без `--sales-ground-truth` не должно ухудшаться**.
6. **Coverage-инвариант**: all raw rows → final (unassigned=0), дублей нет.

## 5. Команды

```bash
# Backfill одной даты (LLM 27b, workers 1, retries 2):
docker exec daily-stats-scheduler \
  python3 /app/offline_analysis/run.py \
    --input /reports/transcript_report_<DATE>.xlsx \
    --output /reports/final_transcript_report_<DATE>.xlsx \
    --workers 1 --retries 2

# GT-эксперимент (08-27):
docker exec daily-stats-scheduler \
  python3 /app/offline_analysis/run.py \
    --input /reports/transcript_report_2026-08-27.xlsx \
    --output /reports/final_transcript_report_2026-08-27.xlsx \
    --sales-ground-truth "/reports/Документы продаж за 27.08.2026г.xlsx" \
    --workers 1 --retries 2

# Валидация finals (в контейнере, есть openpyxl):
docker exec daily-stats-scheduler python3 -c "
import openpyxl, sys
sys.path.insert(0, '/tmp')
import importlib.util
spec=importlib.util.spec_from_file_location('vf','/tmp/vf.py')
vf=importlib.util.module_from_spec(spec); spec.loader.exec_module(vf)
"
# (см. §3 для vf.py; в сессии #1 использовался sed 's|REPORTS_DIR…|Path("/reports")|')

# PDF через poppler (хост):
pdfinfo reports/final_transcript_report_<DATE>.pdf | head -3

# Longest dialog-анализ (инструмент из сессии #1, может быть удалён — пересоздать):
python3 /tmp/opencode/long_dialog.py <DATE>
```

## 6. **Порядок работы** для следующей сессии

1. **Бэкап** всех finals → `/tmp/opencode/final_backup_p0_$(date +%Y%m%d_%H%M%S)/`.
2. **ИЗМЕНЕНИЕ 1 (безопасный фикс, не влияет на LLM/качество):** починить метрику `coverage`
   в `run.py:422-423` → `sum(row_count) == total` (см. §3.4). GT-прогон НЕ нужен: это
   только диагностика (реальное покрытие уже полное — подтверждено). После правки
   пересчитать 2-3 даты и убедиться, что `coverage.ok=True` и finals идентичны
   (разница только в `*_debug.json` → поле `coverage`). Это **отдельный коммит**.
3. **GT-прогон БЕЗ изменения 2** (baseline sanity, на свежем финале 08-27):
   зафиксировать recall/precision/NOT_FOUND (цель ≥ 0.325 / ≥ 0.946 / ≤ 39).
4. **ИЗМЕНЕНИЕ 2 (P0, high confidence):** поднять re-сплит на сегменты с 2+ приветствиями
   (см. §3.1.2 + §3.2, count-based trigger). **НЕ** снижать `REVIEW_LONG_SEC` по умолчанию
   (08-29/09-03 показывают, что LLM может честно вернуть `[]`). Отдельный коммит.
5. **GT-прогон ПОСЛЕ изменения 2:** сравнить с п.3. Если recall↑ и precision не падает →
   оставить. Иначе → откатить изменение 2.
6. **ИЗМЕНЕНИЕ 3 (P1, опционально):** adaptive-chunk при 2+ неудачных попытках (перенести из
   `regression_new/lib.py:251` в `segmentation.py:875-910`) — только если §2.3 деградация
   мешает recall. После — валидация.
7. **Валидация** всех 13 дат после ЛЮБОГО изменения: `validate_finals.py` + `pdfinfo` +
   `sum(row_count)==total` (новый coverage).
8. **Правило «НЕ трогать»**: `prompt.py:26-70` (`SEGMENTATION_SYSTEM_PROMPT`) — первичная
   сегментация. Она работает OK. Не трогать, пока не найдено конкретное нарушение.

## 7. Конкретные места в коде (diff-ориентир)

```
offline_analysis/segmentation.py:503     REVIEW_LONG_SEC = 30*60          (НЕ менять сразу)
offline_analysis/segmentation.py:506     MAX_RESEGMENT_ATTEMPTS = 3
offline_analysis/segmentation.py:600     def resplit_long_segments(...)   (добавить force_review)
offline_analysis/segmentation.py:637     for seg in segments:             (условие входа)
offline_analysis/segmentation.py:642     if dur < review_min:             (добавить OR marks>=2)
offline_analysis/segmentation.py:646     is_very_long = dur >= very_min   (не трогать)
offline_analysis/segmentation.py:657-688 (retry loop, добавить degraded_flag)
offline_analysis/segmentation.py:875-910 (чанк retry loop, adaptive)

offline_analysis/prompt.py:88            RESEGMENT_LONG_SYSTEM_PROMPT     (усилить count-based)
offline_analysis/prompt.py:151           build_long_dialog_resegment_prompt (добавить счётчики)
offline_analysis/prompt.py:26            SEGMENTATION_SYSTEM_PROMPT       (НЕ трогать)

offline_analysis/run.py:422-423          coverage_ok = set(covered)==range(total) (ПОЧИНИТЬ: → sum(row_count)==total, см. §2.4/§3.4)
offline_analysis/taxonomy.py:51          CONFIDENCE_MIN = 0.6             (не трогать)
offline_analysis/run.py:74-75            PIPELINE_NUM_CTX/PREDICT         (не трогать)
offline_analysis/run.py:931              --confidence-min                 (не трогать)
offline_analysis/run.py:949              --sales-ground-truth             (не трогать)
segmentation.py:742-745                  assert per-store invariant        (НЕ ре-базировать индексы в глобальные)

offline_analysis/sales_gt.py             GT-режим (не трогать; использовать для проверки)
regression_new/lib.py:251                _segment_store_adaptive          (перенести в prod, см. §3.3)
```

## 8. Файлы сессии #1 (могут быть удалены — пересоздать)

- `/tmp/opencode/run_batch.sh`, `/tmp/opencode/run_batch2.sh` — batch-драйверы.
- `/tmp/opencode/check_date.py` — быстрая статистика по дате.
- `/tmp/opencode/long_dialog.py` — разбор самого длинного диалога (GREET/PAY маркеры).
- `/tmp/opencode/batch_logs/`, `/tmp/opencode/batch2_logs/` — логи.
- `/tmp/opencode/final_backup_20260904_161738/` — бэкап.

## 9. Что НЕЛЬЗЯ делать

- Не менять prompt/thresholds/алгоритм **ради того, чтобы тест прошёл** (требование AGENTS.md).
- Не рефакторить/переносить `offline_analysis/*.py` **кроме** перечисленных в §7.
- Не создавать дубликаты `BigBudgetLLM` (уже есть в `run.py`; `regression_new/lib.py` — дубль, удалить из нового кода при переносе adaptive-chunk).
- Не менять production-поведение без явной задачи.
