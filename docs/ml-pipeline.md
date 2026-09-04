# ML-pipeline: VAD + ASR (Triton)

В системе **два** независимых Triton'а (GPU, device `0`).
Оба собираются из внешних образов и не входят в этот репозиторий —
compose только запускает их (`docker-compose.yml:227-242` (ASR),
`244-281` (VAD)).

## 1. VAD (voice activity detection)

**Сервер:** `vad_server:latest`, container_name
`server_triton-speech-segmentation`, порт `8001`, `shm_size: 4g`,
GPU device `0`.
Настройки Triton:
- `--model-repository=/mnt/models`
- `--load-model marblenet`
- `--load-model online_vad`
- `--load-model offline_vad`
- onnxruntime, 1 intra/inter thread, `execution_mode=1`,
  global threadpool (docker-compose.yml:258-272).
- `--model-control-mode=explicit` — модели загружаются явно.

**Клиент (в Docker):** `client/client_vad_service.py`.
- Модель: `online_vad` (строка 22), URL
  `server_triton-speech-segmentation:8001` (строка 23).
- Inference — sequence-режим Triton
  (`sequence_id = uuid` — строка 97, `sequence_start` при первом
  чанке сессии — строки 102-103, `sequence_end` всегда False —
  строка 75).
- Вход: `audio [1, N] INT16`, `threshold [1,1] FP16 = 0.2`,
  `min_silence_ms [1,1] INT16 = 500`,
  `mode [1,1] BYTES = "ONLY_SPEECH"`
  (`client_vad_service.py:47-59`).
- Выход: protobuf `Response{ va_marks: [VoiceActivityMark] }`, где
  `mark_type` = 1 (BEGIN) или 2 (END)
  (`vad_pb/output_pb2.py`, разбор в `client_vad_service.py:119-123`).
- По `BEGIN` gateway создаёт **свой** session_id вида
  `<YYYYmmdd-HHMMSS Moscow>-<base>-<uuid>`
  (строки 125-134); по `END` — закрывает «речевую сессию» и чистит
  состояние (156-161).

**Что делает клиент VAD**
- Получает `MicChunk` от PC-клиента по gRPC `AudioBridge` (`:6000`).
- Каждый чанк → `online_vad` → если «в речи» — пробрасывает
  `AudioChunk` в storage worker `worker:50051` (метод `StreamAudio`);
  в «тишине» ничего не шлёт (139-154).
- Отправляет обратно клиенту `VadEvent` c `is_begin/is_end`
  (165-174).

> `marblenet` и `offline_vad` в этом контуре **не используются** —
> только `online_vad`. Названия моделей подтверждены в
> TRITON_PARAMETERS.

## 2. ASR (автоматическое распознавание речи)

**Сервер:** `asr:latest`, container_name `triton-asr`, GPU device `0`.
Модель репозитория — EMformer-конформер (Whisper-подобная
architecture, online-режим). Наименование моделей задаётся в
коде клиента (см. ниже); **в образе** должно уже лежать
model-repository, включая эти модели. Конкретный путь/состав
модели-хранилища из этого репозитория не установить
  — **Требует проверки**.

**Клиент (asr_worker):** `asr_worker/triton_client.py`.
- URL: `triton-asr:8001` (строка 21).
- Онлайн-модель: `emformer_conformer_online_baseline_v1` (строка 14).
- Финал-модель: `emformer_conformer_online_finalize_baseline_v1`
  (строка 15).
  (В комментариях строки 11-12 — варианты `..._tdt_punct_...`,
  закомментированные.)
- Выход: protobuf
  `SpeechRecognitionHypothesis`
  (`asr_worker/proto/output_pb2.py`) с полями `transcript`,
  `normalized_transcript`; берётся
  `hyp.normalized_transcript or hyp.transcript`
  (`triton_client.py:134`).
- Inference: `stream_infer(inputs_iterator)`
  (triton_client.py:82-95) — по одной очереди на сессию;
  `sequence_id = abs(hash(session_id)) & 0x7FFFFFFF`
  (38-41); `interim_results: True` (75); последний чанк с
  сессией — `sequence_end=True` (73).

## 3. Полный ML-путь (от микрофона до текста)

```
PC-клиент  (MicChunk, 16 кГц PCM16, ~150 мс на чанк)
  │   gRPC AudioBridge.StreamMic  [host:6000]
  ▼
vad-client (client_vad_service)
  │   tritonclient.grpc → triton «online_vad» (sequence)
  │   threshold=0.2, min_silence_ms=500, ONLY_SPEECH
  │   BEGIN/END → генерация session_id
  │   gRPC AudioIngestion.StreamAudio  [worker:50051]
  ▼
storage_worker
  │   PCM16 → WAV (16 кГц mono)
  │   put_object  →  MinIO: audio/<session_id>/NNNNNN.wav
  │   Kafka producer  →  audio_events {session_id, s3_key, chunk_id, is_end}
  ▼
asr_worker
  │   Kafka consumer audio_events
  │   MinIO get_object  →  WAV → PCM16
  │   SessionBuffer.add (порядок, восстановление дырок)
  │   pop_if_ready(min_ms=160)  →  куски ~2.56 КБ
  │   tritonclient grpc.aio stream_infer
  │       model = online / online_finalize (по is_last)
  │       →  SpeechRecognitionHypothesis (interim & final)
  │   Kafka producer asr_transcripts {text, is_final, ...}
  │   PostgreSQL UPSERT transcripts (recognition_text, is_final)
  ▼
(дальше — classification / alerts / reports, см. docs/)
```

## 4. Очерёдность и «дупли/дыры»

- `SessionBuffer` (`asr_worker/session_buffer.py`) — держит
  `pending[chunk_id]` для пропущенных чанков, при получении
  «правильного» `chunk_id` — подтягивает из pending (75-81).
  Дубликаты отбрасываются (56-61).
- Финализация сессии — когда `expected_chunk_id > last_chunk_id`
  (session_buffer.py:144-155) и в буфере есть все чанки.

## 5. GPU / требования

- Оба Triton'а резервируют `NVIDIA_VISIBLE_DEVICES: "0"` и
  `device_ids: ["0"]` (docker-compose.yml:233, 277).
  `shm_size: 4g` (для VAD). `ulimits: memlock=-1, stack=-1`
  (VAD).
- `NVIDIA Container Toolkit` установлен на хосте — **обязательно**.
- VAD: onnxruntime backend (1 intra/inter thread).
- ASR: GPU — внутри образа `asr:latest` (настройка видна по
  `deploy.resources.reservations.devices` в compose).

## 6. Диагностика

```bash
# VAD
docker compose logs -f vad
docker compose exec vad trserver_healthcheck 2>/dev/null || \
  curl -s http://localhost:8001/v2/health/ready
# список моделей (через скрипт):
docker compose exec vad tritonserver --model-repository /mnt/models -v 2>&1 | head
# ASR
docker compose logs -f asr
curl -s http://triton-asr-host:8001/v2/health/ready   # из хоста
# изнутри сети:
docker compose run --rm asr curl -s triton-asr:8001/v2/health/ready

# проверить, что модели загружены:
python scripts/check_triton.py        # для ASR (localhost:8002 — см. скрипт)
```

> `scripts/check_triton.py:3` — `localhost:8002` — расхождение
> с compose (8001). **Требует проверки** / отредактировать скрипт.
