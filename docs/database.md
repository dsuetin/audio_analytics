# PostgreSQL

## 1. Описание

- Изображение `postgres:16`, БД `speech_db`, пользователь `speech`,
  пароль `speech` (docker-compose.yml:197-210).
- Данные — named volume `postgres_data`
  (docker-compose.yml:208-209, 391-393).
- Проброшен порт `5432`.
- pgAdmin: `dpage/pgadmin4`, порт `5050` на хосте
  (docker-compose.yml:213-225), логин/пароль по умолчанию из
  compose (`PGADMIN_DEFAULT_EMAIL`, `PGADMIN_DEFAULT_PASSWORD`) —
  **сменить перед прод-запуском**.

## 2. Схема таблицы

Единственная таблица — `transcripts`. DDL описан в `README.md:68-79`:

```sql
CREATE TABLE transcripts (
    session_id TEXT PRIMARY KEY,
    store_id TEXT,
    seller_id TEXT,
    recognition_text TEXT,
    dialog_type TEXT,
    is_sale BOOLEAN NOT NULL DEFAULT FALSE,
    is_alarm_triggered BOOLEAN NOT NULL DEFAULT FALSE,
    is_final BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

> **Важно:** SQL-миграций в репозитории **нет** (нет `migrations/`,
> `alembic`/`flyway`). Таблица должна быть создана вручную по DDL из
> README до/после первого запуска — **Требует проверки**: нет ли
> `init.sql`/volumes на производственном сервере.

## 3. Кто что делает с таблицей

| Операция | Кто | Код | Что пишутся/читается |
|---|---|---|---|
| INSERT / UPSERT (первичная запись) | `asr_worker` | `asr_worker/repo.py:38-73` | `session_id` (PK), `store_id`, `seller_id`, `recognition_text`, `is_final`; `ON CONFLICT (session_id) DO UPDATE` — текст обновляется до финального |
| UPDATE `dialog_type` | `classification_service` | `classification/service.py:237-246` | текущий `label` (buy/service/complaint/...) |
| UPDATE `client_id` | `classification_service` | `service.py:249-258` | `client_N` при новом диалоге |
| UPDATE `is_alarm_triggered=TRUE` | `alert_service` | `alert_service/alerts_service.py:123-133` | при фразе-триггере |
| UPDATE `is_sale=TRUE` | `alert_service` | `alerts_service.py:135-144` | при фразе покупки |
| UPDATE `seller_id` | `alert_service` | `alerts_service.py:147-161` | при смене продавца (`"имя_фамилия"`) |
| SELECT (отчёт) | `stats_service` (scheduler / export) | `stats_service/export_daily_transcript_report.py`, `scheduler.py` | все колонки за дату |
| DELETE (утилита) | `scripts/delete_from_db_today.py` | — | очистка текущего дня |

Сводка по таблице:

| Таблица | Назначение | Кто пишет | Кто читает |
|---|---|---|---|
| `transcripts` | 1 строка = 1 VAD-сессия (фраза/диалоговый фрагмент): текст + метки (магазин, продавец, клиент, тип диалога, покупка, тревога) | `asr_worker` (INSERT/UPSERT); `classification_service` (dialog_type, client_id); `alert_service` (is_sale, is_alarm_triggered, seller_id) | `stats_service` (отчёты), pgAdmin, `scripts/*` |

> Примечание: классификатор и alert-сервис пишут в строку **по
> session_id**, т.е. одна сессия = одна строка. Если `asr_worker`
> упал/не дописал — записи не будет, и `UPDATE` затронет 0 строк.

## 4. Связи

Формальный FK-конstraints в DDL нет. Связность на уровне данных:
- `session_id` — общий ключ для всех сервисов;
- `store_id`/`seller_id` — извлекаются из session_id
  (`asr_worker/repo.py:7-24`), либо передаются
  producer'ом `asr_transcripts`;
- `client_id` — внутренний номер клиента в магазине
  (`client_1`, `client_2`, ...; `classification/service.py:136-137`,
  `stats_service/client_numbering.py`).

## 5. Connection strings

| Сервис | Переменная | Значение (compose) |
|---|---|---|
| asr_worker | `POSTGRES_DSN` | `postgresql://speech:speech@postgres:5432/speech_db` (main.py:57-61) |
| classification | `POSTGRES_HOST/PORT/USER/PASSWORD/DB` + `POSTGRES_DSN` | service.py:73-79 (используются отдельные переменные) |
| alert_service | те же + `POSTGRES_DSN` | alerts_service.py:95-101 |
| stats_service | `POSTGRES_*` | compose x-daily-stats (25-29) |

## 6. Проверка подключения

```bash
# из хоста, в сеть docker:
docker compose exec postgres psql -U speech -d speech_db -c "\dt"
docker compose exec postgres psql -U speech -d speech_db \
  -c "SELECT count(*), max(created_at) FROM transcripts;"

# или локально:
docker run --rm -it --network <project>_speech postgres:16 \
  psql -h postgres -U speech -d speech_db -c "\dt"
```

## 7. Примечания

- UPSERT в `asr_worker` **не обновляет** `store_id`/`seller_id` при
  конфликте (repo.py:60-64) — они пишутся один раз, при INSERT.
  Смена продавца после этого — отдельный `UPDATE` от alert_service.
- `is_final` — признак финального текста сессии (последний
  ASR-хypothesis с `sequence_end`).
- `created_at` — время первого INSERT (default `NOW()`), далее не
  изменяется.
