# Сервисы: детальное описание

Каждый раздел ссылается на конкретные файлы кода.

---

### 1. storage worker (`worker`)

**Назначение**
gRPC-инжест аудио. Получает чанки PCM (`pcm_s16le`, 16 кГц, mono),
оборачивает каждый чанк в WAV и асинхронно кладёт в MinIO. Параллельно
публикует событие `audio_chunk_saved` в Kafka.
`storage_worker/server.py:153-239`.

**Input**
gRPC `AudioIngestion.StreamAudio(stream AudioChunk) returns (StreamAck)`
(`proto/audio.proto:5-7`). Поле `audio` — PCM16.

**Output**
- MinIO: `audio/<session_id>/<chunk_id:06d>.wav`
  (`storage_worker/server.py:182`).
- Kafka topic из `KAFKA_EVENTS_TOPIC` (по умолчанию/в compose — `audio_events`):
  JSON `{type, session_id, timestamp_ms, payload:{s3_key, size_bytes, chunk_id, is_end}}`
  (`storage_worker/events.py:9-22`, `storage_worker/server.py:198-207`).
- gRPC `StreamAck` в ответ на стрим.

**Dependencies**
MinIO (`S3_ENDPOINT_URL`), Redpanda (`KAFKA_BOOTSTRAP_SERVERS`).
`depends_on: [redpanda, minio]` (`docker-compose.yml:114-116`).

**Communication**
gRPC server (`grpc.aio.server`, `server.py:250`), boto3 s3
(`storage_worker/s3.py`), aiokafka producer
(`storage_worker/kafka_events.py:17-22`, producer стартует в `start()` и
посылает через `send_and_wait` с `key=session_id`).

**Configuration**
`S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET`
(`storage_worker/config.py:7-10`); `KAFKA_BOOTSTRAP_SERVERS`,
`KAFKA_EVENTS_TOPIC` (config.py:12-13); `GRPC_HOST`/`GRPC_PORT`
(config.py:15-16); `MIN_PART_SIZE_BYTES`, `MAX_BUFFER_BYTES`
(opределены в config, но не используются в текущем server.py —
**Требует проверки**: вероятно — legacy).

**Runtime**
`python -m storage_worker.server`
(`storage_worker/Dockerfile` `CMD`; compose `worker.build`).

**Healthcheck**
Нет healthcheck'а в compose. Сигналы — логи `service_ready`
(`server.py:77`), `s3_upload_complete` (server.py:118-123),
`s3_upload_failed` (server.py:124-125).

**Logs**
`docker compose logs -f worker`.
Критичные: `s3_upload_failed`, `kafka_error`

---

### 2. VAD gateway (`vad-client`)

**Назначение**
Промежуточный gRPC-сервис в Docker-контуре. Принимает `AudioBridge.StreamMic`
от клиента и:
1. отправляет каждый чанк в Triton (модель `online_vad`, sequence-inference);
2. по `VA_MARK_BEGIN/END` формирует «разговорную сессию» (свой session_id вида
   `<timestamp_moscow>-<base>-<uuid>`);
3. транзитом шлёт «речевые» чанки в storage worker.
`client/client_vad_service.py:85-174`.

**Input**
gRPC `AudioBridge.StreamMic` (`proto/bridge.proto`).

**Output**
- `VadEvent`-стрим (`session_id, sequence, is_begin, is_end`) обратно
  клиенту (`bridge.proto:17-21`, `client_vad_service.py:169-174`).
- Чанки `AudioChunk` → `worker:50051` (`client_vad_service.py:143-154`,
  адрес `worker:50051` захардкожен — `client_vad_service.py:180`).

**Dependencies**
Triton VAD `server_triton-speech-segmentation:8001`
(`client_vad_service.py:23`; имя хоста в compose — `vad`),
`worker` (gRPC :50051). `depends_on: [worker]`
(`docker-compose.yml:157-158`).

**Communication**
gRPC client (Triton, `tritonclient.grpc`), gRPC server (`[::]:6000`),
gRPC client (worker). Параметры VAD: `threshold=0.2`,
`min_silence_ms=500`, `mode=ONLY_SPEECH` (`client_vad_service.py:47-49`).

**Configuration**
Внутри контейнера адреса захардкожены (`URL=...`, `worker:50051`);
compose задаёт `STORAGE_HOST: worker:50051`, но в коде используется
`worker:50051` напрямую (не `STORAGE_HOST`) — расхождение,
**Требует проверки**.

**Runtime**
`python -m client.client_vad_service` (`client/Dockerfile`).

**Healthcheck**
Нет. Вывод `🔥 VAD CLIENT STARTED` (`client_vad_service.py:193`).

**Logs**
`docker compose logs -f vad-client`.

---

### 3. asr_worker

**Назначение**
Распознавание речи. Подписан на `audio_events`; по событию
`audio_chunk_saved` скачивает WAV из MinIO, реасамблерует чанки
(`SessionBuffer`), посылает PCM в Triton EMformer-конформер
(interim + final), публикует `asr_transcripts`, пишет тексты в Postgres.
`asr_worker/main.py:108-231`.

**Input**
Kafka `audio_events` (consumer group `asr-worker-v1`,
`auto_offset_reset="earliest"` — `asr_worker/consumer.py:14-22`).

**Output**
- Kafka `asr_transcripts`:
  `{session_id, store_id, seller_id, chunk_id, text, is_final}`
  (`asr_worker/main.py:85-95`; producer `acks=all` —
  `asr_worker/producer.py:11-15`).
- Postgres: UPSERT в `transcripts`
  (`asr_worker/repo.py:38-73`).

**Dependencies**
Kafka, MinIO (S3), PostgreSQL, Triton ASR `triton-asr:8001`
(`asr_worker/triton_client.py:21`). `depends_on: [redpanda, minio]`.

**ASR-модели**
`emformer_conformer_online_baseline_v1` (interim),
`emformer_conformer_online_finalize_baseline_v1` (final) —
`asr_worker/triton_client.py:14-15`. Sequence-inference с
`sequence_id = abs(hash(session_id)) & 0x7FFFFFFF` (triton_client.py:38-41),
`interim_results: True` (triton_client.py:75).

**Configuration**
`POSTGRES_DSN` (main.py:57-61); S3 endpoint/key **захардкожены**
(`main.py:40-45`) — env `S3_ENDPOINT` из compose не читается
(Требует проверки).

**Runtime**
`python -m asr_worker.main` (`asr_worker/Dockerfile`).

**Healthcheck**
«POSTGRES POOL STARTED», «🔥 PRODUCER STARTED», «🚀 CONSUMER STARTED»
в логах.

**Бюфер/поведение**
`SessionBuffer` (`asr_worker/session_buffer.py`) — упорядочивание чанков
по `chunk_id`, восстановление «дырок», `pop_if_ready(min_ms=160)`,
`is_end_ready`, `pop_all`. Заполненный буфер отдаётся ASR кусками
~160 мс (`main.py:207-210`). Финализация — когда все чанки до
`last_chunk_id` получены (session_buffer.py:144-155), после чего
`pop_all` + `asr.send(... is_last=True)` (main.py:176-191).

**Logs**
`docker compose logs -f asr_worker`. Ключевые:
`s3_read session=...`, `ASR FINAL`, `DB SAVE`, `failed processing asr event`.

---

### 4. classification_service

**Назначение**
Classifциация диалога по фразам/лексемам. Читает `asr_transcripts`,
поддерживает состояние магазина/клиентов/сессий, считает «очки»
миссий, публикует `classified_events` (и `new_client_session`), пишет
`dialog_type` в Postgres.
`classification/service.py:116-264`.

**Input**
Kafka `asr_transcripts` (group `classification-service` —
`classification/policy.py:6`). Чутье: любой чанк с текстом
(`service.py:124`).

**Output**
- Kafka `classified_events`
  (`{session_id, store_id, chunk_id, is_final, text, label, score, mode, counters, matched_words}`
  — `service.py:96-107`), `mode` = `threshold` (первое превышение) или
  `switch` (смена сценария).
- Kafka `new_client_session` (при начале нового диалога —
  `service.py:145-153`).
- Postgres `UPDATE transcripts SET dialog_type`
  (`service.py:237-246`).

**Logic**
- `find_phrases` — лексические фразы
  (`classification/phrase_matcher.py`); гистограмма
  `session_state.partial` → при `is_final` переносится в
  `client_state.confirmed` (`classification/classifier.py:31-50`).
- `score` суммирует очки по миссиями из `MISSIONS`
  (`classification/policy.py:8-182`: buy, service, complaint, corporate,
  working_hours, vacancy, help, lost, other).
- `best_label` — mаx по очкам (classifier.py:100-111).
- Порог `THRESHOLD = 1` (policy.py:185).
- «Новый клиент» — `DialogSession.process`:
  прощание (FAREWELLS) закрывает сессию, приветствие (GREETINGS)
  после закрытия начинает новую
  (`classification/dialog_sessions.py:1-51`; вызов — `service.py:131`).
- Состояние: `StateManager` → `StoreState` →
  `ClientState`/`SessionState` (state.py, client_state.py,
  session_state.py, `store_state.current_client_id` — service.py:130-137).

**Dependencies**
Kafka + Postgres. `depends_on: [redpanda]`.
(В compose не `depends_on postgres`, но `service.py:73-79`
подключается к БД в `start()` — **Требует проверки**: при падении
Postgres на старте сервис упадёт.)

**Configuration**
`KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_INPUT_TOPIC`, `KAFKA_OUTPUT_TOPIC`,
`POSTGRES_*` / `POSTGRES_DSN` (`classification/policy.py:1-6`,
`service.py:26-79`).

**Runtime**
`python -m classification.service` (Дockerfile, compose `command`
не переопределён).

**Logs**
`docker compose logs -f classification-service`.
`🔥 CLASSIFICATION STARTED`, `EMIT ...` (service.py:95,109),
`New client ...`, гистограммы в stdout.

---

### 5. alert_service

**Назначение**
Business-правила поверх транскриптов: «возражение/триггер»
(`TRIGGER_PHRASES`), «покупка» (`BUY_PHRASES`), «смена продавца»
(`SALESPERSON_PHRASES` → финальный фрагмент, извлечение
имени+фамилии). Публикует `alerts`, `purchases`, `salesperson_changes`,
пишет флаги в Postgres, шлёт Telegram-уведомления по триггерам.
`alert_service/alerts_service.py:176-347`.

**Input**
Kafka `asr_transcripts` (group `alerts-service`).

**Output**
- Kafka `alerts`: `objection_trigger`
  (`alerts_service.py:217-238`).
- Kafka `purchases`: `purchase` (267-282, **одно** событие
  на сессию — `purchase_sessions` set, строка 58, 259-264).
- Kafka `salesperson_changes`: `salesperson_change`
  (297-347).
- Postgres: `is_alarm_triggered`, `is_sale`, `seller_id`
  (`alerts_service.py:123-161`).
- Telegram: `TelegramBot.send_message` в `TELEGRAM_CHAT_ID`
  (`alerts_service.py:105-125`, `telegram_bot.py`).

**Logic и пороги**
- Совпадение фраз — `find_phrase` (stemming + до `MAX_GAP=5` слов
  между словами фразы: `text_matcher.py`, `config.py:79`).
- Покупка: любая из `BUY_PHRASES` (config.py:43-69). Порядок
  проверок в `handle()`: сначала триггер (возврат `return` —
  `alerts_service.py:203-246`), затем покупка (253-290), затем
  смена продавца (294-347). Если в том же сообщении сработал
  триггер — покупку в этом сообщении проверять не будут
  (**Требует проверки**: осознанное решение или нет).
- Смена продавца: триггер-фраза кладёт session в `pending_salesperson`;
  на `is_final` извлекает 2 слова после «имя продавца / имя
  консультанта» → `new_salesperson = "first_last"` (314-347).
- Защита от дублей: `self.fired_sessions`, `self.purchase_sessions`
  (in-memory; при перепуске контейнера сбрасываются — **Требует
  проверки**).

**Dependencies**
Kafka + Postgres + Telegram API (external). `depends_on: [redpanda, postgres]`.

**Configuration**
`KAFKA_INPUT_TOPIC`, `KAFKA_ALERT_TOPIC`, `KAFKA_PURCHASE_TOPIC`,
`KAFKA_SALESPERSON_TOPIC` (через env: alerts_service.py:31-49;
значения из `.env` через `env_file: .env`).
`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` (56-74).
`POSTGRES_*` / `POSTGRES_DSN`.

**Runtime**
`python -m alert_service.alerts_service`.

**Healthcheck**
`🚨 ALERT SERVICE STARTED`. Без Telegram-токена — `RuntimeError`
(63-64), т.е. сервис не стартует без `TELEGRAM_TOKEN`.

**Logs**
`docker compose logs -f alert-service`.
`🚨 ALERT`, `💰 PURCHASE`, `👤 SALESPERSON`.

---

### 6. daily_stats_scheduler

**Назначение**
Ежедневная генерация raw-отчёта и затем LLM-анализ.
`stats_service/scheduler.py`.

**Input**
Postgres `transcripts`, дата = «вчера» в `REPORT_TIMEZONE`
(`scheduler.py:22-24`).

**Output**
- `REPORT_OUTPUT_DIR/transcript_report_<date>.xlsx` + `.pdf`
  (через `export_daily_transcript_report.py`).
- `REPORT_OUTPUT_DIR/final_transcript_report_<date>.xlsx`
  (через `offline_analysis/run.py` + LLM).
  `scheduler.py:26-27, 156`.

**Пайплайн (порядок)**
1. Если `--run-on-startup` или `--once` — сразу (321-333).
2. Иначе — ждёт полуночи в `REPORT_TIMEZONE`, затем цикл
   (335-354).
3. `run_daily_report` → subprocess `export_daily_transcript_report.py`
   (36-81) → проверка `ensure_raw_report_created` (111-126) →
   `run_offline_analysis` (136-225): если `final` уже валиден —
   skip (159-167), иначе subprocess `offline_analysis/run.py`
   (169-203) → `validate_final_report` (228-256).
4. Ошибка raw ⇒ `RuntimeError` (91-93), offline не запускается.
   Ошибка offline ⇒ raw остаётся (205-209, 212-215).

**Dependencies**
Postgres, Ollama (для offline), `offline_analysis` пакет, `stats_service`
пакет. `OFFLINE_ANALYSIS_ENABLED` default 1 (compose 38).
Объёмы: `./reports:/reports`, `./stats_service:/app/stats_service`,
`./offline_analysis:/app/offline_analysis` (compose 16-22).

**Env**
`POSTGRES_*`, `REPORT_TIMEZONE` (default Europe/Moscow),
`REPORT_OUTPUT_DIR` (default `/reports` в контейнере),
`OLLAMA_HOST` (default `http://172.18.0.1:11434`),
`OFFLINE_LLM_MODEL` (default `qwen3.8:27b`), `OFFLINE_ANALYSIS_ENABLED`,
`OFFLINE_WORKERS` (default 1), `OFFLINE_LLM_TIMEOUT` (default 3600),
`FORCE_OFFLINE` (scheduler.py:155), `LOG_LEVEL` (313).

**Runtime**
`python3 -m stats_service.scheduler` (compose command, строка 321).

**Healthcheck**
Логи: `Running report for ...`, `RAW REPORT: OK | ...`,
`OFFLINE ANALYSIS: OK | ...`, `PIPELINE: DONE for ...`
(scheduler.py:98, 224-225).

**Logs**
`docker compose logs -f daily_stats_scheduler`.

---

### 7. daily_stats_api

**Назначение**
FastAPI: выдача готовых final-отчётов. `stats_service/api.py`.

**Endpoints** (api.py:250-277):

| Method/Path | Параметры | Возврат | Код |
|---|---|---|---|
| `GET /` | — | HTML-страница выбора даты | 250-252 |
| `GET /reports/daily` | `report_date: date` (query) | `final_transcript_report_<date>.xlsx` (или 404) | 255-277 |

FastAPI подхватывает `date` из query; `OpenAPI/Swagger` доступно
по `/docs` — встроенный FastAPI.

**Dependencies**
`REPORT_OUTPUT_DIR` (default `/reports`). Файл должен уже быть
создан scheduler'ом (или вручную).

**Runtime**
`uvicorn stats_service.api:app --host 0.0.0.0 --port 8000`
(compose 329-335).

**Переходы**
`http://<host>:8000/docs`, `http://<host>:8000/?date=2026-01-01`.

---

### 8. windows_autorun (клиент на ПК магазина)

**Назначение**
- Подача микрофона в VAD-gateway (`gRPC :6000`);
- чтение 6 тем Kafka (в т.ч. `classified_events`,
  `new_client_session`, `alerts`, `purchases`, `salesperson_changes`)
  и печать в лог/консоль;
- GUI-окно статуса (tkinter) — `gui_status_window.py`, snapshot
  через `client_status.py`;
- «watchdog»/heartbeat (heartbeat-файл, `AppClock`, `SessionMonitor`)
  + pid-lock + auto-restart при разрыве.
  `windows_autorun/client.py`.

**Input**
Микрофон (sounddevice), `.env`:
`SERVER_IP`, `STORE_ID`, `WORKER_NAME`, `AUDIO_DEVICE`
(`client.py:57-60`, `windows_autorun/.env.example`).

**Output**
- gRPC `MicChunk` → `:6000` (606-738).
- Логи: `logs/client.log` (rotating) + `logs/client_heartbeat`
  (356-367, 66-67).
- GUI snapshot (client_status.py).

**Ключевое поведение**
- Consumer group `mic-client-<STORE_ID>` (66) — уникальная на магазин,
  at-least-once (manual commit, `enable_auto_commit=True` +
  `auto_commit_interval_ms=0` + `commit` после успешной обработки
  пакета — 878-909).
- На `salesperson_changes` — `WORKER_NAME = new_salesperson`
  (815-818) и `stop_current_session()` (820) — смена session_id.
  Это и есть момент смены продавца на клиенте.
- На `alerts` — `🚨`, на `purchases` — `💰` (809-823).

**Runtime**
`python client.py` (или install.bat/start.bat — см.
`windows_autorun/README.md`).

**Healthcheck**
Файл `logs/client_heartbeat` (обновляется каждые 30 сек; при
зависании НЕ обновляется — 328-376).

---

### 9. Тестовые / демонстрационные клиенты

| Файл | Назначение |
|---|---|
| `client/mic_client.py` | live-микрофон → bridge `:6000`; читает 6 тем и печатает (161-279) |
| `client/wav_client.py` | подаёт WAV-файлы из `samples/*.wav` (79-109, 129-167) |
| `client/example_client.py` | подаёт `AUDIO_PATH` напрямую в storage worker `:50051`, минуя VAD |
| `scripts/play_client.py` | (см. скрипт) — воспроизведение/демо |
| `scripts/check_triton.py` | диагностика репо моделей ASR-Triton |
| `scripts/download_and_play_audio_by_session_id.py` | скачать WAV с MinIO по session_id и воспроизвести |
| `scripts/delete_from_db_today.py` | удалить текущий день из `transcripts` |

Эти клиенты **не являются частью production-контура**, но
проверяют pipeline и полезны для отладки.
