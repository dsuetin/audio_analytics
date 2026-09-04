# Поток данных (data flow)

Подробная схема — в `docs/architecture.md` (разделы 2–3) с
диаграммами. Здесь — сводка каждого перехода с реальными
payload'ами из кода.

## Переход 1. Микрофон → VAD gateway

- Кого касается: `windows_autorun/client.py` →
  `client/client_vad_service.py`.
- Протокол: gRPC bidi-stream `AudioBridge.StreamMic`
  (`proto/bridge.proto`), порт `6000` (host) / `:6000`
  (внутри compose — `vad-client:6000`).
- Данные: `MicChunk { session_id, audio: PCM16 mono 16 кГц,
  sample_rate, is_begin, is_end }` — чанки ~150 мс
  (`CHUNK_MS=150`, `windows_autorun/client.py:78`).
- Триггер следующего этапа: каждый чанк (realtimе).

## Переход 2. VAD gateway → storage worker

- Протокол: gRPC `AudioIngestion.StreamAudio`
  (`proto/audio.proto`), адрес `worker:50051`
  (`client/client_vad_service.py:180`).
- Данные: `AudioChunk { session_id (новый VAD-формат), sequence,
  audio, sample_rate, is_begin, is_end, encoding=pcm_s16le }`
  (`client_vad_service.py:143-152`).
- Условие: отправляется **только** при «речи» по
  `online_vad` (mark BEGIN..END) — в тишине чанки丢弃аются
  (client_vad_service.py:139).
- Сессия-метка: новый `session_id` =
  `<YYYYmmdd-HHMMSS Moscow>-<STORE_ID>-<WORKER_NAME>-<uuid>`
  (client_vad_service.py:126-130 + windows_autorun/client.py:606-609).

## Переход 3. storage worker → MinIO + Kafka

- MinIO: `PUT audio/<session_id>/<chunk_id:06d>.wav`
  (storage_worker/server.py:180-187) — WAV из PCM
  (server.py:26-35).
- Kafka: topic `audio_events`, message:
  ```json
  {"type":"audio_chunk_saved","session_id":"...","timestamp_ms":123,
   "payload":{"s3_key":"audio/.../000001.wav","size_bytes":4800,
              "chunk_id":1,"is_end":false}}
  ```
  (storage_worker/server.py:198-217, `events.py`).
- Триггер: каждый принятый чанк; `is_end=true` — закрытие сессии
  (server.py:227-232).

## Переход 4. asr_worker: Kafka + MinIO → Triton → Kafka + Postgres

- Выхват: topic `audio_events` (consumer group `asr-worker-v1`).
- Триггер: любое сообщение `type=="audio_chunk_saved"`
  (`asr_worker/main.py:108-117`).
- С3: `GET audio/<session>/<chunk>.wav` (`main.py:121-123`,
  `s3_client.py`).
- ASR: Triton `triton-asr:8001`, модели
  `emformer_conformer_online_baseline_v1` (interim) и
  `..._finalize_baseline_v1` (по `is_end`), sequence-inference
  (`triton_client.py`).
- Выход Kafka `asr_transcripts`:
  ```json
  {"session_id": "...", "store_id": "main_store",
   "seller_id": "иванов_иван", "chunk_id": 3,
   "text": "добрый день", "is_final": true}
  ```
  (`asr_worker/main.py:85-95`); `store_id`/`seller_id` — из
  session_id (`repo.py:7-24`).
- Выход Postgres: UPSERT `transcripts` по `session_id`
  (`repo.py:38-73`).

## Переход 5. classification_service: asr_transcripts → classified_events + Postgres

- Consumer: `asr_transcripts` (group `classification-service`).
- Логика: фразы → гистограмма → очки миссий → `best_label`;
  состояние магазина/клиента в памяти
  (`classification/service.py:116-264`, `classifier.py`,
  `policy.py`, `dialog_sessions.py`).
- Выход Kafka `classified_events`:
  ```json
  {"session_id": "...", "store_id": "...", "chunk_id": 7,
   "is_final": true, "text": "...", "label": "buy",
   "score": 4, "mode": "threshold",
   "counters": {"buy":4,"service":1}, "matched_words": {"buy":{"цена":1}}}
  ```
  (`service.py:96-107`).
- Выход Kafka `new_client_session`:
  `{"type":"new_session","store_id":"...","client_id":"client_2"}`
  (service.py:145-153).
- Выход Postgres: `UPDATE transcripts SET dialog_type=...` /
  `SET client_id=...`.

## Переход 6. alert_service: asr_transcripts → alerts/purchases/salesperson_changes + Postgres + Telegram

- Consumer: `asr_transcripts` (group `alerts-service`).
- Триггеры (порядок важен — `alerts_service.py:203-347`):
  1. `TRIGGER_PHRASES` (возражение) → topic `alerts`,
     Telegram, `is_alarm_triggered=TRUE`, `return`.
  2. `BUY_PHRASES` (покупка) → topic `purchases`, `is_sale=TRUE`,
     `return`.
  3. `SALESPERSON_PHRASES` + финальный фрагмент → topic
     `salesperson_changes`, `seller_id=...`.
- Формат:
  ```json
  {"session_id":"...","store_id":"...","text":"слишком дорого",
   "phrase":"слишком дорого","type":"objection_trigger"}
  ```
```json
  {"session_id":"...","text":"беру","phrase":"беру","type":"purchase"}
  ```
```json
  {"session_id":"...","text":"имя консультанта петров пётр",
   "type":"salesperson_change","new_salesperson":"петров_пётр"}
  ```

## Переход 7. PC-клиент принимает события

- Consumer group `mic-client-<STORE_ID>`, topics:
  `asr_transcripts` (печатает текст, 🏁/⌨), `classified_events`
  (label в консоль), `new_client_session` (👤 Client changed),
  `alerts` (🚨), `purchases` (💰), `salesperson_changes` (👤 +
  `WORKER_NAME` обновляется + `stop_current_session()` → новая
  сессия с новым продавцом). Код: `windows_autorun/client.py:762-840`.
- GUI-окно: `client_status.py` + `gui_status_window.py`
  (только чтение in-process snapshot).

## Переход 8. Отчёт (после, днём)

- `daily_stats_scheduler`: Postgres → Excel/PDF → (opt.)
  Ollama `qwen3.8:27b` → `final_...xlsx`. Файлы в `./reports`
  (хост) = `/reports` (контейнер).
- `daily_stats_api`: `GET /reports/daily?report_date=...` → файл.
  (stats_service/api.py:255-277).

## Сводная таблица «кто-кому-что»

| # | Producer | Channel | Consumer | Данных |
|---|---|---|---|---|
| 1 | PC-клиент | gRPC :6000 | vad-client | MicChunk (PCM) |
| 2 | vad-client | gRPC :50051 | storage worker | AudioChunk (PCM) |
| 3a | storage worker | MinIO | asr_worker | WAV-чанки |
| 3b | storage worker | Kafka `audio_events` | asr_worker | `{s3_key, chunk_id, is_end}` |
| 4a | asr_worker | Triton asr:8001 | EMformer | PCM16 (chunk 160 мс) |
| 4b | asr_worker | Kafka `asr_transcripts` | classification, alert, PC-клиент | `{text, is_final}` |
| 4c | asr_worker | Postgres | (reports) | transcripts (insert) |
| 5a | classification | Kafka `classified_events` | PC-клиент | `{label, score, mode}` |
| 5b | classification | Kafka `new_client_session` | PC-клиент | `{client_id}` |
| 5c | classification | Postgres | (reports) | transcripts (update) |
| 6a | alert | Kafka `alerts` / `purchases` / `salesperson_changes` | PC-клиент | события бизнес-уровня |
| 6b | alert | Telegram | человек | текст уведомления |
| 6c | alert | Postgres | (reports) | is_sale, is_alarm, seller_id |
| 7 | scheduler | Postgres→Ollama→FS | (human) | отчёты xlsx/pdf |
