# Деплой и запуск

## 1. Требования к хосту

| Что | Зачем | Обязательность |
|---|---|---|
| Docker Engine + Docker Compose v2 | контейнеры | обязателен |
| NVIDIA GPU + **NVIDIA Container Toolkit** | 2 Triton'а (VAD, ASR) резервируют `device_ids: ["0"]` | обязателен |
| ~1 ГБ RAM для Redpanda (`--memory 1G`) + память на Triton/Python-сервисы | стабильность | обязателен |
| Свободные порты | список ниже | обязателен |
| Оllama + модель `qwen3.8:27b` (и GPU/CPU) | offline-анализ отчёта | опционален (выключается `OFFLINE_ANALYSIS_ENABLED=0`) |
| Доступ в интернет | Telegram API, `redpandadata/redpanda`, `minio` и т.д. при сборке | обязателен |

Порты, используемые compose (host): `9092, 19092, 9644, 9000, 9001, 8080, 5432, 5050, 50051, 6000, 8001, 8000`.

## 2. Обязательные внешние образы

В этом репозитории **нет** Dockerfile'ов для двух образов —
compose ссылается на уже собранные:

| Изображение | Откуда |
|---|---|
| `asr:latest` | внешний build (EMformer + model repository) — **не в этом репозитории** |
| `vad_server:latest` | внешний build (`/mnt/models`: `marblenet`, `online_vad`, `offline_vad`) — **не в этом репозитории** |

> Если этих образов нет на хосте — `docker compose up` подтянет
> «not found». **Не удалось определить по репозиторию**, как их
> собрать (нет Dockerfile/CI). Спросите у владельцев ML-пакета.

## 3. Конфигурация

### 3.1 Репозиторий

Все переменные либо в `docker-compose.yml`, либо в файле `.env`
на хосте рядом с compose (только через `${VAR}` —
`docker-compose.yml:35-42`, `378-379`).

**Реальные значения секрета в `.env` НЕ копировать.** Ниже —
только имена переменных и смысл.

| Переменная | Сервис | Обязательность | Значение по умолчанию | Смысл |
|---|---|---|---|---|
| `TELEGRAM_TOKEN` | alert_service (`env_file: .env`) | **обязательна** | — | без неё сервис не стартует (`alerts_service.py:63-64`) |
| `TELEGRAM_CHAT_ID` | alert_service (`env_file: .env`) | **обязательна** (иначе алерты не уедут) | — | чат уведомлений |
| `MAX_BOT_TOKEN`, `MAX_CHAT_ID` | alert_service (compose 378-379) | **неизвестно** | — | переменные не используются в коде `alert_service`; **Требует проверки** |
| `OLLAMA_HOST` | daily_stats_scheduler | опц. | `http://172.18.0.1:11434` | адрес Ollama; из сети compose loopback не доступен, отсюда 172.18.0.1 (default bridge gateway) |
| `OFFLINE_LLM_MODEL` | daily_stats_scheduler | опц. | `qwen3.8:27b` | модель |
| `OFFLINE_ANALYSIS_ENABLED` | daily_stats_scheduler | опц. | `1` | `0` — только raw-отчёт |
| `OFFLINE_WORKERS` | daily_stats_scheduler | опц. | `1` | параллельность LLM |
| `OFFLINE_LLM_TIMEOUT` | daily_stats_scheduler | опц. | `3600` | сек на запрос |
| `POSTGRES_HOST/PORT/USER/PASSWORD/DB` | postgres, все service | опц. (в compose заданы жёстко `speech/speech/speech_db`) | — | для локального запуска — `localhost` вместо `postgres` |
| `REPORT_TIMEZONE` | daily_stats_* | опц. | `Europe/Moscow` | TZ расчёта полуночи |
| `REPORT_OUTPUT_DIR` | daily_stats_* | опц. | `./reports` (хост), `/reports` (container) | где лежат xlsx/pdf |
| `KAFKA_*_TOPIC` | services | опц. (в compose заданы) | — | см. `docs/kafka.md` |
| `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET` | worker | опц. (в compose заданы) | localhost:9000 / minioadmin / audio-sessions | MinIO |
| `GRPC_HOST`, `GRPC_PORT` | worker | опц. | 0.0.0.0 / 50051 | gRPC |
| `MIN_PART_SIZE_BYTES`, `MAX_BUFFER_BYTES` | worker | **Требует проверки** — не используются в текущем `server.py` | 5 МБ / 64 МБ | legacy (multipart), см. `storage_worker/config.py:18-19` |
| `FORCE_OFFLINE` | scheduler | опц. | `false` | проигнорировать idempotency-final |
| `LOG_LEVEL` | scheduler | опц. | `INFO` | уровень лога |

### 3.2 Windows-клиент

`windows_autorun/.env.example` — шаблон; заполнить на каждом ПК
магазина:

| Переменная | Обязательность | Смысл | Пример |
|---|---|---|---|
| `SERVER_IP` | **обязательна** | IP сервера (для gRPC :6000 и Kafka :19092) | `172.16.20.111` |
| `STORE_ID` | **обязательна** | идентификатор магазина | `first_store` |
| `WORKER_NAME` | **обязательна** | имя продавца (меняется голосовой командой на сервере) | `иванов_иван` |
| `AUDIO_DEVICE` | опц. | числовой индекс или имя микрофона; пусто = default Windows | `Logi C525` |

### 3.3 Что **не** настроить

- Kafka topics — создаются автоматически при первом publish.
- MinIO bucket `audio-sessions` — **не создаётся кодом**;
  создайте вручную: `mc mb myminio/audio-sessions` (иначе
  `NoSuchBucket` в `worker`). **Требует проверки**, есть ли CI-шаг.
- PostgreSQL-таблица `transcripts` — нет миграций в репозитории;
  выполните DDL из `README.md:68-79` вручную или убедитесь,
  что в `postgres_data`-volume она уже есть.

## 4. Запуск

Правильный порядок (один и тот же compose-файл):

```bash
# 1) убедиться, что внешние образы есть:
docker images | egrep "asr:latest|vad_server:latest"
#    если нет — получить их у владельцев ML (см. раздел 2).

# 2) (один раз) создать bucket:
docker run --rm -it --network <project>_speech minio/mc \
  sh -c "mc alias set m http://minio:9000 minioadmin minioadmin && mc mb -p m/audio-sessions"

# 3) (один раз) создать таблицу, если volume пустой:
docker compose exec -T postgres psql -U speech -d speech_db <<'SQL'
CREATE TABLE IF NOT EXISTS transcripts (
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
SQL

# 4) (один раз) в .env рядом с compose задать TELEGRAM_TOKEN / TELEGRAM_CHAT_ID

# 5) up:
docker compose up -d --build

# 6) убедиться, что все контейнеры Up:
docker compose ps
```

> Порядок startup внутри compose: Redpanda + MinIO + Postgres
> стартуют первыми; `worker` `depends_on` redpanda+minio;
> `vad-client` `depends_on` worker; остальное — без жёсткого
> порядка, но при старте сервисы сами коннектятся. **Нет**
> `healthcheck:` в compose — «depends_on» не ждёт готовности
> зависимостей (только started).

### 4.1 Порядок для локальной разработки (без GPU)

Если на хосте нет GPU — ASR и VAD не запустятся. Варианты:
- поднять compose **без** `asr` и `vad`:
  `docker compose up -d redpanda minio postgres worker vad-client asr_worker classification_service alert_service daily_stats_scheduler daily_stats_api`
  (VAD gateway будет падать — без `online_vad` он бесполезен).
- поднять compose **без ML**, и поднимать VAD/ASR только
  когда GPU доступен — самый честный вариант (иначе pipeline
  не e2e).

## 5. Проверка запуска

### 5.1 Контейнеры

```bash
docker compose ps
docker compose logs --tail 50
```

Ключевые строки, которые **должны** быть:

| Сервис | Строка |
|---|---|
| worker | `service_ready`, `gRPC server started` |
| vad-client | `🔥 VAD CLIENT STARTED` |
| asr_worker | `🚀 CONSUMER STARTED`, `POSTGRES POOL STARTED`, `🔥 PRODUCER STARTED` |
| classification_service | `🔥 CLASSIFICATION STARTED` |
| alert_service | `🚨 ALERT SERVICE STARTED` |
| daily_stats_scheduler | `Sleeping X.X seconds until midnight` (или `Running report for ...` при `--once`) |

### 5.2 Инфраструктура

```bash
# Kafka (Redpanda) up:
docker compose exec redpanda rpk cluster info 2>/dev/null | head
# topics:
docker compose exec redpanda rpk topic list
# консоль: http://localhost:8080

# MinIO:
curl -sf http://localhost:9000/minio/health/live && echo OK
# console: http://localhost:9001

# Postgres:
docker compose exec postgres psql -U speech -d speech_db -c "SELECT version();"
# console (pgAdmin): http://localhost:5050

# Stats API:
curl -s http://localhost:8000/docs
curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:8000/reports/daily?report_date=2026-08-27"

# Triton VAD:
curl -s http://localhost:8001/v2/health/ready

# gRPC health: отсутствие ошибок `gRPC error:` в логах клиентов.
```

### 5.3 Smoke test (end-to-end, без живого микрофона)

```bash
# 1) подготовить тестовый WAV в samples/
# 2) запустить wav_client (внутри docker-сети или на хосте,
#    где есть localhost-пробросы 6000 + 19092):
docker compose run --rm --network speech python -m client.wav_client
#    — подаст WAV-файлы на VAD gateway и дождётся final ASR.

# 3) убедиться, что в MinIO появились объекты:
docker compose exec minio \
  mc alias set m http://localhost:9000 minioadmin minioadmin
docker compose exec minio mc ls -r m/audio-sessions/ | tail

# 4) убедиться, что в transcripts появилась запись:
docker compose exec postgres psql -U speech -d speech_db \
  -c "SELECT session_id, seller_id, recognition_text, is_final FROM transcripts ORDER BY created_at DESC LIMIT 3;"

# 5) убедиться, что event прошёл через pipeline:
docker compose logs asr_worker | egrep "ASR FINAL|s3_read|DB SAVE"
docker compose logs classification-service | egrep "EMIT|label="
docker compose logs alert-service | egrep "ALERT|PURCHASE|SALESPERSON"
```

### 5.4 Полный сценарий (продавец)

1. PC-клиент поднят, GUI говорит «Система работает».
2. Продавец говорит «здравствуйте ...».
3. В логе PC-клиента: `🟢 SPEECH START` → строки `🖨 ...` (interim) →
   `🏁 ...` (final).
4. В Postgres — строка с `is_final=true`, `recognition_text` заполнен.
5. В `classified_events` (виден в Redpanda Console) — текущий `label`.
6. Фраза «слишком дорого» → в Telegram приходит 🚨; в Postgres
   `is_alarm_triggered=true`.
7. Фраза «купить аккумулятор/беру» → в Postgres `is_sale=true`,
   в логе PC-клиента 💰.
8. Продавец говорит «имя консультанта Петров Пётр» + пауза →
   Postgres `seller_id="петров_пётр"` (lowercase), в PC-клиенте
   строка 👤 и новая сессия.
9. В конце дня — `reports/transcript_report_<date>.xlsx`
   и `final_transcript_report_<date>.xlsx`.

## 6. Остановка

```bash
docker compose down          # оставляет volumes (data)
docker compose down -v       # также удаляет minio_data / postgres_data
```

## 7. Обновление кода

Обычные сервисы (`worker`, `vad-client`, `asr_worker`,
`classification_service`, `alert_service`, `daily_stats_*`) собираются
из кода репозитория — после `git pull` достаточно:

```bash
docker compose build \
  worker vad-client asr_worker classification_service alert_service \
  daily_stats_scheduler daily_stats_api
docker compose up -d
```

> Локальный `--build` при `docker compose up -d` — тоже работает
> (в compose уже `build:`). Но явное `build` видно в логе.
