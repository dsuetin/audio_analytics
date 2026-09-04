# Kafka / Redpanda

## 1. Развёртывание

Один нода Redpanda в Docker, `--overprovisioned`:
`redpandadata/redpanda:latest` (`docker-compose.yml:46-79`).

- **INTERNAL** listener: `INTERNAL://redpanda:9092` — для
  контейнеров сети `speech`.
- **EXTERNAL** listener: `EXTERNAL://172.16.20.111:19092`
  (advertised) — для клиентов на ПК магазинов (`client.py:62`:
  `KAFKA_BOOTSTRAP = f"{SERVER_IP}:19092"`).
- Проброшены порты: 9092, 19092, 9644 (admin API).
- Redpanda Console — отдельный контейнер на порту 8080
  (`docker-compose.yml:134-145`).

> **Важно:** advertised EXTERNAL адрес жёстко `172.16.20.111`
> (docker-compose.yml:71). При переносе/смене IP сервера его нужно
> отредактировать, иначе внешние клиенты не смогут подключаться.

## 2. Все topics (только реально используемые в коде)

| Topic | Producer | Consumer | Что передаётся | Назначение |
|---|---|---|---|---|
| `audio_events` | `storage_worker` — `KAFKA_EVENTS_TOPIC` (`server.py:198-217`, `kafka_events.py`) | `asr_worker` — `KafkaConsumerWrapper(topic="audio_events")` (`main.py:235-238`) | `{type:"audio_chunk_saved", session_id, timestamp_ms, payload:{s3_key, size_bytes, chunk_id, is_end}}` | «чанк аудио записан в S3, скачай и распознай» |
| `asr_transcripts` | `asr_worker` — `main.py:85-95` | `classification_service` (`KAFKA_INPUT_TOPIC`), `alert_service` (`KAFKA_INPUT_TOPIC`), PC-клиент, `wav_client` | `{session_id, store_id, seller_id, chunk_id, text, is_final}` | распознанный текст (interim + final) |
| `classified_events` | `classification_service` — `service.py:111-114` | PC-клиент (`mic_client.py:196`, `windows_autorun/client.py:785`) | `{session_id, store_id, chunk_id, is_final, text, label, score, mode(threshold\|switch), counters, matched_words}` | текущий label диалога для UI |
| `new_client_session` | `classification_service` — `service.py:145-153` | PC-клиент (`mic_client.py:223`, `windows_autorun/client.py:803`) | `{type:"new_session", store_id, client_id}` | «начался новый диалог» |
| `alerts` | `alert_service` — `alerts_service.py:227-238` (env `KAFKA_ALERT_TOPIC`) | PC-клиент (`windows_autorun/client.py:809`) | `{session_id, store_id, text, phrase, type:"objection_trigger"}` | возражение клиента |
| `purchases` | `alert_service` — `alerts_service.py:276-282` (env `KAFKA_PURCHASE_TOPIC`) | PC-клиент | `{session_id, store_id, text, phrase, type:"purchase"}` | покупка зафиксирована |
| `salesperson_changes` | `alert_service` — `alerts_service.py:331-340` (env `KAFKA_SALESPERSON_TOPIC`) | PC-клиент (`windows_autorun/client.py:809-822`) | `{session_id, store_id, text, type:"salesperson_change", new_salesperson}` | смена продавца; клиент обновляет `WORKER_NAME` и рестартит сессию |

Topics создаются Redpanda автоматически при первом publish
(автоматическое создание включено по умолчанию) — в репозитории
нет явного `kafka-topics create`.

Названия по умолчанию в коде: `asr_worker/producer.py` (topic
передаётся при вызове), `classification/policy.py:4-5`,
`alert_service/alerts_service.py:33-49`, `.env`.

## 3. Consumer groups

| Group | Who | auto_offset_reset | Commit |
|---|---|---|---|
| `asr-worker-v1` | asr_worker | `earliest` (`asr_worker/consumer.py:18-19`) | default (auto) |
| `classification-service` | classification_service | `latest` (`service.py:61`) | default (auto) |
| `alerts-service` | alert_service | `latest` (`alerts_service.py:83`) | default (auto) |
| `mic-client-<STORE_ID>` | windows_autorun/client.py | `latest` (`client.py:104-114`) | manual: `enable_auto_commit=True, auto_commit_interval_ms=0` + явный `commit` после успешной пачки (`client.py:876-886`); при ошибке — offset НЕ коммитится, redelivery (at-least-once) |
| `wav-client` | client/wav_client.py | `latest` | default (auto) |
| `mic-client` | client/mic_client.py (тестовый) | `latest` | default (auto) |

> `asr_worker` с `earliest` — новые consumer'ы переобрабатывают всю
> историю `audio_events`, включая «чужие» сессии из прошлого.
> **Требует проверки** — возможно, это осознанный выбор (восстановление
> после сбоя), но для новых магазинов это лишняя нагрузка.

## 4. Поведение при сбоях

- **storage_worker (producer)**: `retry_backoff_ms=500`,
  `request_timeout_ms=30000` (`kafka_events.py:17-22`).
  При ошибке `send` ловится `_kafka_worker`
  (`server.py:140-143`) — чанк **не** сохраняется повторно
  (S3-объект уже мог лечь). `logger.exeption` — bug
  (`kafka_events.py:43`), из-за которого при сбое лог-сообщение
  упадёт с `AttributeError`.
- **asr_worker (consumer)**: простой цикл `async for msg in consumer`
  (consumer.py:29-31); при исключениях в `handle_event` задача
  `process_session` создаётся отдельно — обработка не блокирует
  consumption.
- **windows_autorun**: полный reconnect-loop с exponential backoff
  (до 30 с), poison-message escalation (`client.py:896-946`),
  «rebalance/reset» обрабатываются отдельным ветвлением.
- **classification / alert**: нет явного retry; `aiokafka`
  сам переподключает consumer; при падении контейнера —
  `restart: unless-stopped`.

## 5. Протокол сообщений

Все payload'ы — JSON UTF-8 (`ensure_ascii=False` в storage_worker,
`json.dumps` в остальных). Ключ партиционирования:
`storage_worker` — `session_id` (`kafka_events.py:40`);
остальные — без ключа (round-robin партиции).
