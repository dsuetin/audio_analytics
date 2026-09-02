# Audio Analytics

Система аудиоаналитики для магазинов: слушает разговоры через
микрофон, распознаёт речь, классифицирует диалоги, фиксирует
покупки, возражения и смену продавцов — и формирует ежедневные
отчёты (Excel/PDF + LLM-анализ).

## Архитектура (кратко)

```
Микрофон (Windows-клиент)
  → gRPC VAD gateway (online_vad / Triton)
  → storage worker (gRPC :50051 → MinIO: audio/<session>/NNNNNN.wav)
  → Kafka topic audio_events
  → asr_worker (Triton EMformer → текст)
  → Kafka asr_transcripts + PostgreSQL (transcripts)
  → classification_service (dialog_type) + alert_service
    (покупка / возражение / смена продавца → Telegram)
  → daily stats: Excel/PDF отчёты + Ollama (qwen3.8:27b)
```

Диаграммы (flow + sequence) и детальный разбор — в
`docs/architecture.md`.

## Сервисы

| Сервис | Код | Роль |
|---|---|---|
| `worker` | `storage_worker/` | gRPC-инжест аудио → WAV → MinIO, события `audio_events` |
| `vad-client` | `client/client_vad_service.py` | gRPC-гейтвей `AudioBridge` + VAD (Triton `online_vad`) |
| `asr_worker` | `asr_worker/` | S3 → Triton EMformer → `asr_transcripts` + Postgres |
| `classification_service` | `classification/` | классификация диалога (`classified_events`) |
| `alert_service` | `alert_service/` | покупка/возражение/смена продавца, Telegram |
| `daily_stats_scheduler` | `stats_service/scheduler.py` | ежедневный raw-отчёт + LLM-анализ |
| `daily_stats_api` | `stats_service/api.py` | HTTP-API выдачи final-отчётов (`:8000`) |
| (вне compose) | `windows_autorun/client.py` | клиент на ПК магазина: микрофон + лог/GUI |
| (вне compose) | `offline_analysis/` | LLM-анализ дневного Excel (Ollama) |

Инфраструктура (Docker): Redpanda (Kafka), MinIO (S3), PostgreSQL 16,
pgAdmin, Redpanda Console, два Triton с GPU (`asr`, `vad`).

Подробно по каждому — в `docs/services.md`.

## Быстрый запуск

Требования: Docker + Compose v2, NVIDIA GPU + NVIDIA Container Toolkit
(два Triton'а), готовых образы `asr:latest` и `vad_server:latest`
собираются **внешне**, не в этом репозитории, Ollama (опционально,
для LLM-шага отчёта).

```bash
# 0) секреты — рядом с docker-compose.yml:
#    .env  →  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
#    (значения по умолчанию в .env — заменить на свои, не коммитить токены)

docker compose up -d --build
docker compose ps          # все Up
```

До первого запуска нужно один раз:

1. Создать бакет MinIO `audio-sessions` (кодом не создаётся).
2. Создать таблицу `transcripts` (DDL — в `docs/database.md`;
   миграций в репозитории нет).

Полная инструкция с проверками — в `docs/deployment.md`.

## Проверка работоспособности

```bash
docker compose ps
docker compose logs --tail 50 worker asr_worker classification-service alert-service
curl -s http://localhost:8000/docs                                   # stats API
curl -s http://localhost:8001/v2/health/ready                        # Triton VAD
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:9000/minio/health/live
docker compose exec postgres pg_isready -h postgres -U speech
```

MinIO Console: `http://localhost:9001`, Redpanda Console:
`http://localhost:8080`, pgAdmin: `http://localhost:5050`.

Smoke-тест end-to-end без живого микрофона (WAV-файл):
`docs/deployment.md` → «Smoke test».

## Где что найти

| Документ | Содержимое |
|---|---|
| `docs/architecture.md` | архитектура, диаграммы (Mermaid), сеть/порты |
| `docs/services.md` | описание каждого сервиса |
| `docs/data-flow.md` | переход «кто → кому → по какому каналу → что» |
| `docs/kafka.md` | Redpanda, все topics, consumer groups |
| `docs/database.md` | таблица `transcripts`, кто пишет/читает |
| `docs/storage-s3.md` | MinIO, bucket, ключи объектов |
| `docs/ml-pipeline.md` | VAD + ASR, модели, параметры Triton |
| `docs/api.md` | gRPC-контракты, HTTP API, Telegram |
| `docs/deployment.md` | запуск, конфигурация, smoke-тест |
| `docs/troubleshooting.md` | диагностика по сценариям |
| `docs/USER_GUIDE.md` | памятка для продавцов |

## Troubleshooting

`docs/troubleshooting.md` — разборы: контейнер не стартует,
Kafka/Postgres/MinIO/Triton недоступны, текст не попадает в БД,
алерт не срабатывает, смена продавца не работает, отчёт не
генерируется, PC-клиент молчит.

Быстрый «светофор» — там же, раздел 13.

## Ключевые env-переменные

| Переменная | Для чего | Где |
|---|---|---|
| `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` | **обязательны** для `alert_service` | `.env` (корень) |
| `SERVER_IP`, `STORE_ID`, `WORKER_NAME`, `AUDIO_DEVICE` | PC-клиент магазина | `windows_autorun/.env` |
| `OLLAMA_HOST`, `OFFLINE_LLM_MODEL`, `OFFLINE_ANALYSIS_ENABLED` | LLM-шаг дневного отчёта | `.env` (корень), default в compose |
| `REPORT_TIMEZONE`, `REPORT_OUTPUT_DIR` | отчёты | compose (default Europe/Moscow, `./reports`) |

Полный список с дефолтами — `docs/deployment.md` раздел 3.

## Логи

- Сервисы: `docker compose logs -f <service>`.
- PC-клиент: `windows_autorun/logs/client.log` +
  `logs/client_heartbeat` (watchdog).
- Отчётные файлы: `./reports/` (`transcript_report_<date>.xlsx/pdf`,
  `final_transcript_report_<date>.xlsx`, `*_debug.json`).
