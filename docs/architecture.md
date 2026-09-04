# Архитектура системы Audio Analytics

Документ описывает реальную архитектуру на основе кода репозитория.
Каждая связь подтверждена конкретным файлом и строкой.

## 1. Общий принцип

Система — конвейер обработки аудио из магазина:

```
Микрофон (Windows-клиент)
    → gRPC (VAD-gateway)
    → gRPC (storage worker)
    → S3/MinIO (первый фрагмент аудио — WAV)
    → Kafka/Redpanda (topic audio_events)
    → ASR worker (читает S3, распознаёт через Triton/EMformer)
    → Kafka (topic asr_transcripts) + PostgreSQL (таблица transcripts)
    → классификация dialog_type (topic classified_events) + UPDATE transcripts
    → алерты / покупка / смена продавца (topics alerts, purchases, salesperson_changes) + UPDATE transcripts
    → ежедневный отчёт из PostgreSQL (Excel/PDF + LLM-анализ через Ollama)
```

Вся realtime-обработка асинхронная, событийная: аудио лежит в S3,
по Kafka ходят только события и тексты, состояние — в PostgreSQL.

## 2. Диаграмма взаимодействия сервисов

```mermaid
flowchart LR
    subgraph PC[Компьютер магазина - Windows]
        MC[windows_autorun/client.py<br/>звук с микрофона]
        MC -- "gRPC AudioBridge.StreamMic<br/>{SERVER_IP}:6000" --> VADGW
        MC <. "Kafka consumer group mic-client-STORE_ID<br/>{SERVER_IP}:19092<br/>asr_transcripts, classified_events,<br/>new_client_session, alerts, purchases,<br/>salesperson_changes" .> RP
    end

    subgraph DC[Docker-контур на сервере]
        VADGW[vad-client<br/>client/client_vad_service.py<br/>gRPC :6000 → :50051]
        TritonV[vad / Triton<br/>модели marblenet, online_vad,<br/>offline_vad · :8001]
        VADGW -- "Triton gRPC infer online_vad<br/>server_triton-speech-segmentation:8001" --> TritonV
        VADGW -- "gRPC AudioIngestion.StreamAudio<br/>worker:50051" --> SW
        SW[storage worker<br/>gRPC :50051]
        SW -- "boto3 put_object" --> MINIO[(MinIO<br/>bucket audio-sessions<br/>audio/&lt;session&gt;/NNNNNN.wav)]
        SW -- "producer topic audio_events" --> RP
        AW[asr_worker]
        RP[Redpanda/Kafka<br/>INTERNAL :9092 · EXTERNAL :19092]
        AW -- "consumer audio_events (g=asr-worker-v1, earliest)" --> RP
        AW -- "get_object" --> MINIO
        AW -- "producer asr_transcripts (acks=all)" --> RP
        AW -- "asyncpg INSERT/UPDATE" --> PG
        TritonA[asr / Triton<br/>EMformer-конформер<br/>triton-asr:8001]
        AW -- "Triton gRPC stream_infer<br/>emformer_conformer_online_baseline_v1 /<br/>..._finalize_baseline_v1" --> TritonA
        CS[classification_service]
        CS -- "consumer asr_transcripts (g=classification-service)" --> RP
        CS -- "producer classified_events,<br/>new_client_session" --> RP
        CS -- "UPDATE transcripts" --> PG
        AS[alert_service]
        AS -- "consumer asr_transcripts (g=alerts-service)" --> RP
        AS -- "producer alerts, purchases,<br/>salesperson_changes" --> RP
        AS -- "UPDATE transcripts" --> PG
        AS -- "Telegram Bot API" --> TG[Telegram<br/>chat ID из env]
        PG[(PostgreSQL 16<br/>speech_db.transcripts)]
        DS[daily_stats_scheduler<br/>python -m stats_service.scheduler]
        DS -- "SELECT transcripts<br/>за вчерашний день" --> PG
        DS -- "subprocess: export + offline_analysis" --> LLM[Ollama<br/>OLLAMA_HOST, модель qwen3.8:27b]
        DA[daily_stats_api<br/>uvicorn :8000]
        DA -- "GET /reports/daily → файл из ./reports" --> VOL[host-volume ./reports<br/>= /reports]
    end

```

## 3. Диаграмма последовательности (основной сценарий)

```mermaid
sequenceDiagram
    participant S as Продавец/Клиент
    participant C as windows_autorun/client.py
    participant V as vad-client (gRPC :6000)
    participant TV as Triton (online_vad :8001)
    participant W as storage worker (gRPC :50051)
    participant M as MinIO
    participant K as Redpanda
    participant A as asr_worker
    participant TA as Triton (EMformer)
    participant P as PostgreSQL
    participant CL as classification_service
    participant AL as alert_service
    participant T as Telegram

    C->>V: StreamMic(MicChunk) [PCM16 mono 16 кГц, ~150 мс на чанк]
    loop каждый чанк
        V->>TV: infer("online_vad", threshold=0.2, min_silence_ms=500, ONLY_SPEECH)
        TV-->>V: Response.va_marks (BEGIN/END)
        V->>W: StreamAudio(AudioChunk) только при «в речи»
        W->>M: put_object audio/&lt;session&gt;/NNNNNN.wav
        W->>K: audio_events {type,session_id,payload{s3_key,chunk_id,is_end}}
    end
    K->>A: audio_events (chunk)
    A->>M: get_object (WAV → PCM)
    A->>A: SessionBuffer.add (упорядочивание по chunk_id)
    A->>TA: stream_infer (online-модель, interim_results)
    TA-->>A: SpeechRecognitionHypothesis (текст, interim)
    A->>K: asr_transcripts {session_id,store_id,seller_id,chunk_id,text,is_final}
    A->>P: INSERT/UPDATE transcripts
    K->>CL: asr_transcripts (final)
    CL->>CL: find_phrases → score → best_label, threshold/switch
    CL->>K: classified_events {label, score, mode}
    CL->>P: UPDATE transcripts SET dialog_type
    K->>AL: asr_transcripts (final)
    AL->>AL: проверка TRIGGER_PHRASES / BUY_PHRASES / SALESPERSON_PHRASES
    AL->>T: 🚨 сообщение в чат (только для TRIGGER_PHRASES)
    AL->>K: alerts | purchases | salesperson_changes
    AL->>P: UPDATE transcripts (is_alarm_triggered / is_sale / seller_id)
    C-->>S: печатный вывод в лог/консоль, GUI-окно статуса
```

## 4. Компоненты

### 4.1 Инфраструктурные (изображения)

| Компонент | Изображение | Назначение | Подтверждение |
|---|---|---|---|
| Redpanda (Kafka) | `redpandadata/redpanda:latest` | брокер событий, 1 нод, `--overprovisioned` | `docker-compose.yml:46-79` |
| MinIO | `minio/minio:latest` | объект-хранилище аудио | `docker-compose.yml:81-99` |
| PostgreSQL | `postgres:16` | таблица `transcripts` | `docker-compose.yml:197-210` |
| pgAdmin | `dpage/pgadmin4` | веб-администрирование БД, порт 5050 | `docker-compose.yml:213-225` |
| Redpanda Console | `docker.redpanda.com/redpandadata/console:latest` | веб-интерфейс брокера, порт 8080 | `docker-compose.yml:134-145` |
| Triton (ASR) | `asr:latest` (собственное) | EMformer-конформер | `docker-compose.yml:227-242` |
| Triton (VAD) | `vad_server:latest` (собственное) | `marblenet`, `online_vad`, `offline_vad` | `docker-compose.yml:244-281` |

Изображения `asr:latest` и `vad_server:latest` собираются **не в этом
репозитории** — compose ссылается на готовые образы; модели (`marblenet`,
`online_vad`, `offline_vad`, EMformer-модели) также поставляются внутри
образов (model-repository `/mnt/models` для VAD, путь к модели
EMformer задан внутри образа `asr`). Состав моделей подтверждён строками
`TRITON_PARAMETERS` и имён моделей в коде клиентов.

### 4.2 Сервисы приложения (собственный код репозитория)

| Сервис (compose) | Код | Роль |
|---|---|---|
| `worker` | `storage_worker/` | gRPC-инжест: PCM → WAV → MinIO, события в Kafka |
| `vad-client` | `client/client_vad_service.py` | gRPC-гейтвей `AudioBridge`: VAD-сегментация + прокидывание «речи» дальше |
| `asr_worker` | `asr_worker/` | ASR: S3 → PCM → Triton → текст → Kafka + Postgres |
| `classification_service` | `classification/` | классификация диалога по фразам, диалоги/клиенты |
| `alert_service` | `alert_service/` | возражения / покупка / смена продавца, Telegram |
| `daily_stats_scheduler` | `stats_service/scheduler.py` | ежедневный raw-отчёт + offline LLM-анализ |
| `daily_stats_api` | `stats_service/api.py` | HTTP API выдачи готовых final-отчётов |
| (вне compose) | `windows_autorun/client.py` | клиент на ПК магазина: микрофон → gRPC; Kafka → лог/GUI |
| (вне compose) | `client/mic_client.py`, `client/wav_client.py` | тестовые клиенты (live микрофон, WAV-файлы) |
| (вне compose) | `offline_analysis/run.py` | LLM-анализ дневного Excel (вызывается scheduler'ом) |

### 4.3 Ключевые решения, подтверждённые кодом

1. **Аудио не ходит по Kafka.** По Kafka только события JSON.
   Аудио-данные: gRPC-чанки → WAV в MinIO.
   `storage_worker/server.py:180-187`, `README.md:27-29`.
2. **VAD — на стороне «серверного» gRPC-гейтвея**, а не на клиенте.
   Клиент шлёт сырой поток `MicChunk`; `client_vad_service` сам считает
   `is_begin/is_end` по `online_vad` (mark_type 1/2) и шлёт дальше только
   «речь». `client/client_vad_service.py:119-155`.
3. **Session id — носитель метаданных.**
   Формат, который генерирует `client_vad_service.py:126-130`:
   `<YYYYmmdd-HHMMSS_moscow>-<base>-<uuid>`, где base у PC-клиента —
   `{STORE_ID}-{WORKER_NAME}` (`windows_autorun/client.py:606-609`).
   `store_id`/`seller_id` извлекаются из id в трёх местах:
   `asr_worker/repo.py:7-24`, `classification/metadata.py:1-25`,
   `alert_service/metadata.py:1-25` — формула одна и та же
   (частей ≥ 8: store=parts[2], seller=parts[3:-5]).
4. **Запись в `transcripts` первично делает `asr_worker`**
   (`asr_worker/repo.py:38-73`); classification/alert делают
   `UPDATE ... SET dialog_type/is_sale/is_alarm_triggered/seller_id`.
5. **Один потребитель группы** (на каждую тему) — consumer group
   у каждого сервиса своя: `asr-worker-v1`, `alerts-service`,
   `classification-service`, `mic-client-<STORE_ID>`, `wav-client`.
   `windows_autorun/client.py:66`, `asr_worker/consumer.py:18`,
   `classification/service.py:60`, `alert_service/alerts_service.py:82`.
6. **Отчётная часть полностью отделена от realtime**: scheduler читает
   Postgres, пишет Excel/PDF в `./reports`, LLM-анализ идёт через
   Ollama на hосте (`docker-compose.yml:32-42`, `offline_analysis/llm.py`).

## 5. Где что хранится

| Данные | Физическое расположение | Кто пишет | Кто читает |
|---|---|---|---|
| WAV-чанки аудио | MinIO, bucket `audio-sessions`, `audio/<session_id>/NNNNNN.wav` | `worker` | `asr_worker` |
| Распознанные тексты | PG `speech_db.transcripts` | `asr_worker` (INSERT/UPSERT), `classification_service` и `alert_service` (UPDATE) | scheduler, `stats_service`, `offline_analysis`, pgAdmin |
| События (ephemeral) | Redpanda topics `audio_events`, `asr_transcripts`, `classified_events`, `new_client_session`, `alerts`, `purchases`, `salesperson_changes` | см. `docs/kafka.md` | см. `docs/kafka.md` |
| Готовые отчёты | хост-директория `./reports` (volume → `/reports`) | scheduler / api | `daily_stats_api`, человек |
| Логи клиентов | `windows_autorun/logs/client.log` (rotating, 10×1МБ, 5 бэкапов), heartbeat-файл | windows-клиент | администратор |

## 6. Сеть и порты

Один пользовательский network `speech` для всех сервисов
(`docker-compose.yml:1-3`).

| Порт | Хост | Сервис | Назначение |
|---|---|---|---|
| 9092 | `redpanda:9092` (внутренний) | Redpanda INTERNAL | Kafka API для контейнеров |
| 19092 | `172.16.20.111:19092` (advertised EXTERNAL) | Redpanda EXTERNAL | Kafka API для клиентов на ПК магазинов |
| 9644 | host | Redpanda admin API | — |
| 9000 | host | MinIO S3 API | — |
| 9001 | host | MinIO console | — |
| 8080 | host | Redpanda Console | — |
| 5432 | host | PostgreSQL | — |
| 5050 | host | pgAdmin | — |
| 50051 | host | storage worker gRPC | `AudioIngestion` |
| 6000 | host | vad-client gRPC | `AudioBridge` (вход для клиентов) |
| 8001 | host (у `vad`), в-сети `triton-asr:8001` | двух Triton'ов | VAD: `server_triton-speech-segmentation:8001`; ASR: `triton-asr:8001` |
| 8000 | host | `daily_stats_api` | FastAPI выдачи отчётов |

> Примечание: порт 8001 проброшен только для `vad`; ASR-Triton
> доступен контейнерам по имени `triton-asr` внутри сети `speech`
> (`asr_worker/triton_client.py:21`).

## 7. Известные расхождения и «шероховатости» кода

Помечено так как **Требует проверки** — это факт текущего кода,
но не обязательно осознанное решение:

- `storage_worker/kafka_events.py:43` — опечатка
  `logger.exeption(...)` ⇒ при сбое отправки в Kafka будет
  `AttributeError` внутри обработчика (log не срабатывает).
- `asr_worker/main.py:71` — `S3Client(endpoint="http://minio:9000", ...)`
  захардкожен; env `S3_ENDPOINT` из compose **не используется**.
- `asr_worker/main.py:108-117` — обрабатывается только события
  `type == "audio_chunk_saved"` — единственный тип, который
  публикует `worker`.
- `asr_worker/main.py:207-214` — цикл `process_session` выходит из
  while при `pop_if_ready() → None`, т.е. **до** обработки
  `is_end` (см. `SessionBuffer.is_end_ready`). Фактически
  финализация сессии зависит от того, что `is_end_ready` проверится
  в начале следующей итерации. Точный порядок и условия —
  **Требует проверки** при изменении буфера.
- `alert_service/alerts_service.py:209-246` — обработка
  `TRIGGER_PHRASES` идёт **до** проверки покупки и смены
  продавца, и после срабатывания делает `return` (одна фраза-триггер
  «съедает» событие).
- `alert_service/alerts_service.py:66` — `print("token", token)` —
  в лог уходит TELEGRAM_TOKEN. Рекомендуется убрать.
- `.env` содержит **реальный** Telegram-Bot-Token
  (`alert_service` подхватывает его через `env_file: .env`).
  Токен должен быть выключен из git (`.gitignore` не покрывает `.env`
  в корне — **Требует проверки**).
- `classification/service.py:169-172` — «working»-гистограмма
  суммирует `confirmed` текущего клиента и `partial` всех
  `active_sessions` — состояние общее на магазин, а не на клиент.
  **Требует проверки** при расширении логики.
- `docker-compose.yml:71` — advertised EXTERNAL Kafka адрес жёстко
  `172.16.20.111`; при переносе сервера его нужно поменять,
  иначе ПК-клиенты не подключатся к EXTERNAL-порту.
