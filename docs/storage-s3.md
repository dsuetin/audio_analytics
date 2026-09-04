# S3 / MinIO

## 1. Развёртывание

- Изображение `minio/minio:latest`
  (`docker-compose.yml:81-99`).
- Команда: `server /data --console-address ":9001"`.
- Креденшелы: `MINIO_ROOT_USER=minioadmin`,
  `MINIO_ROOT_PASSWORD=minioadmin` — **по умолчанию**; в
  production заменить.
- Данные — named volume `minio_data:/data`.
- Порты: `9000` (S3 API), `9001` (Web Console).

## 2. Bucket и структура объектов

- Bucket: `audio-sessions`
  (`storage_worker/config.py:10`, compose `S3_BUCKET`).
- В репозитории **нет кода на создание бакета** (нет `make_bucket`).
  Bucket предполагает предсоздание (вручную / mc / через Console).
  **Требует проверки**: при первом запуске на чистой MinIO
  `put_object` упадёт с `NoSuchBucket`, пока нет бакета.
- Ключ объекта: `audio/<session_id>/<chunk_id:06d>.wav`
  (`storage_worker/server.py:182`) — каждый чанк ~150 мс →
  отдельный WAV-объект.
- Формат: WAV, PCM16 mono, 16 кГц
  (`storage_worker/server.py:26-35` — `pcm_to_wav_bytes`).
- ContentType `application/octet-stream`
  (`storage_worker/s3.py:18-22`).

> Примечание: README (`README.md:47-57`) описывает иную схему —
> «один объект на сессию через multipart»
> (`audio/<session_id>.raw`, `s3_multipart`, `session_store`).
> Текущий `server.py` не использует multipart и пишет **по объекта
> на чанк**. Это историческое расхождение — фактический код берёт
> верх. **Требует проверки**: не планируется ли возврат к
> multipart.

## 3. Кто пишет / читает

| Роль | Кто | Код | Метод |
|---|---|---|---|
| Писатель | `storage_worker` | `storage_worker/s3.py:17-22` | `boto3 put_object` |
| Читатель | `asr_worker` | `asr_worker/s3_client.py` (`get_object`) → вызов `main.py:122` | `boto3 get_object` (WAV → `wav_to_pcm`, `main.py:30-35`) |
| Читатель (debug) | `scripts/download_and_play_audio_by_session_id.py`, `scripts/play_client.py` | `boto3` + `simpleaudio` | list + get |

## 4. Credentials

| Сервис | Переменные | Значение (compose) |
|---|---|---|
| storage_worker | `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET` | `http://minio:9000`, `minioadmin`, `minioadmin`, `audio-sessions` |
| asr_worker | захардкожено в `asr_worker/main.py:40-45` | `http://minio:9000`, `minioadmin`, `minioadmin`, `audio-sessions` (env `S3_ENDPOINT` из compose **не** читается — **Требует проверки**) |
| скрипты | захардкожено | `http://localhost:9000`, `minioadmin/minioadmin`, `audio-sessions` |

## 5. Связь с Kafka

MinIO **не публикует** событий. Уведомление о появлении данных — это
Kafka-событие `audio_chunk_saved`, которое `storage_worker` шлёт
**после** постановки объекта в очередь загрузки
(`server.py:187 → 198-217`). Возможная гонка: Kafka-событие может
дойти до `asr_worker` раньше, чем `put_object` физически завершится
(загрузка — асинхронная очередь `_s3_worker`). При `get_object`
в этом случае — `ClientError/NoSuchKey`, которая логгируется как
`failed processing asr event`, а сообщение **не** пересказывается
(потому что `asr_worker` не делает retry на уровне consumer).
**Требует проверки** — есть ли внешняя защита от этой гонки.

## 6. Проверка MinIO

```bash
# S3 API отвечает:
curl -s http://localhost:9000/minio/health/live

# Консоль: http://localhost:9001 (minioadmin/minioadmin)

# Список объектов:
docker run --rm -it --network <project>_speech minio/mc \
  sh -c "mc alias set myminio http://minio:9000 minioadmin minioadmin && \
         mc ls -r myminio/audio-sessions/ | head"

# или из хоста:
aws --endpoint-url http://localhost:9000 \
    --aws-access-key-id minioadmin --aws-secret-access-key minioadmin \
    s3 ls s3://audio-sessions/ --recursive | head
```

## 7. Что делать при проблемах

- `NoSuchBucket` → создать бакет:
  `mc mb myminio/audio-sessions`.
- `AccessDenied` → проверить `MINIO_ROOT_USER/PASSWORD` vs
  `S3_ACCESS_KEY_ID/SECRET`.
- События в Kafka есть, а в S3 объектов нет (и наоборот) →
  смотрите логи `worker`: `queued_s3_upload`, `s3_upload_complete`,
  `s3_upload_failed`, `created_kafka_event`, `sending_kafka_event`.
