# API: gRPC и HTTP

## 1. gRPC-контракты

Два proto-файла (сознательно разделены, см. `README.md:9-25`).
Генерация: `scripts/gen_proto.sh`:

```bash
python -m grpc_tools.protoc -I=proto --python_out=. --grpc_python_out=. proto/audio.proto proto/bridge.proto
```

### 1.1 `proto/audio.proto` — storage contract

```protobuf
service AudioIngestion {
  rpc StreamAudio(stream AudioChunk) returns (StreamAck);
}
message AudioChunk {
  string session_id = 1;
  uint64 sequence = 2;
  bytes audio = 3;            // PCM
  bool is_begin = 4;
  bool is_end = 5;
  uint32 sample_rate = 6;
  uint64 timestamp_ms = 7;
  string encoding = 8;        // e.g. pcm_s16le
}
message StreamAck {
  string session_id = 1;
  uint64 received_chunks = 2;
  uint64 received_bytes = 3;
  string s3_key = 4;
}
```

| Сторона | Кто | Код |
|---|---|---|
| Server | `storage_worker` (`AudioIngestionService`) | `storage_worker/server.py:41-239` |
| Client | `vad-client` (после VAD) | `client/client_vad_service.py:143-171` |
| Client (обходной) | `client/example_client.py` | весь файл — подаёт аудио **минуя VAD** напрямую storage |

### 1.2 `proto/bridge.proto` — VAD contract

```protobuf
service AudioBridge {
  rpc StreamMic(stream MicChunk) returns (stream VadEvent);
}
message MicChunk { string session_id; bytes audio; uint32 sample_rate; bool is_begin; bool is_end; }
message VadEvent { string session_id; uint64 sequence; bool is_begin; bool is_end; }
```

| Сторона | Кто | Код |
|---|---|---|
| Server | `vad-client` (`VADGateway`) | `client/client_vad_service.py:26-174`, слушаю `[::]:6000` |
| Client | PC-клиент (windows_autorun, mic_client) | `windows_autorun/client.py:1024-1036`, `client/mic_client.py:299-312` |

### 1.3 Triton (не protobuf-сервис, но gRPC inference)

| Модель | Клиент | URL |
|---|---|---|
| `online_vad` | `client/client_vad_service.py` | `server_triton-speech-segmentation:8001` |
| `emformer_conformer_online_baseline_v1` / `._finalize_.v1` | `asr_worker/triton_client.py` | `triton-asr:8001` |

Triton-ответы — protobuf:
- `vad_pb/output_pb2.py` — `Response.va_marks[].mark_type ∈ {1=BEGIN,2=END}`;
- `asr_worker/proto/output_pb2.py` — `SpeechRecognitionHypothesis.transcript / normalized_transcript`.

## 2. HTTP API: stats service

FastAPI, контейнер `daily_stats-api`, порт `8000`
(`docker-compose.yml:325-339`, `stats_service/api.py`).

| Endpoint | Метод | Параметры | Ответ | Код |
|---|---|---|---|---|
| `/` | GET | — | HTML-форма выбора даты (JS сам ходит на `/reports/daily`) | `api.py:250-252` |
| `/reports/daily?report_date=YYYY-MM-DD` | GET | `report_date` (query, ISO-дата) | `FileResponse` `final_transcript_report_<date>.xlsx` (media_type `application/zip`) либо 404 | `api.py:255-277` |
| `/docs`, `/openapi.json` | GET | — | Swagger UI / OpenAPI (встроенные FastAPI) | — |

Примеры:

```bash
curl -s "http://localhost:8000/reports/daily?report_date=2026-08-27" -o report.xlsx
# список доступных дат — из каталога:
ls reports/final_transcript_report_*.xlsx
```

> API **не генерирует** отчёт и не ходит в Postgres — отдаёт файл,
> который заранее сгенерировал `daily_stats_scheduler`. 404, если
> файла нет в `REPORT_OUTPUT_DIR`.

## 3. Telegram (входящий/исходящий)

- Исходящие уведомления: `alert_service` →
  `https://api.telegram.org/bot<TELEGRAM_TOKEN>/sendMessage`
  (`alert_service/telegram_bot.py:1-31`), chat_id из
  `TELEGRAM_CHAT_ID`.
- В репозитории есть also `tests/test_max_bot.py` — клиент API
  `platform-api2.max.ru` (Max-мессенджер). **Не задействован** в
  `alert_service` (там подключён `TelegramBot`). **Требует
  проверки**: является ли `MaxBot` планом миграции.

## 4. Redpanda admin API

`http://<host>:9644` — стандартный admin API Redpanda
(проброс в compose, строка 78). Используется для инспекции
topics/consumer groups (`/kafka/v3/clusters/...`).
