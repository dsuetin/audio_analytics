# Troubleshooting

Все проверки опираются на команды, которые реально работают с этим
compose. Не выдумывайте — начинайте с `docker compose ps` и логов.

## 1. Контейнер не поднимается / падает

```bash
docker compose ps
docker compose logs --tail 100 <service>
```

Типичные:
1. **alert_service падает при старте** — не задан канал уведомлений:
   нужен `NOTIFICATION_CHANNEL=telegram|max` (или автоопределение) и
   соответствующие креденшелы (`TELEGRAM_TOKEN`+`TELEGRAM_CHAT_ID` или
   `MAX_BOT_TOKEN`+`MAX_CHAT_ID`): `notifications.py` поднимает `RuntimeError`.
2. **asr_worker не подключается к Postgres** — нет ДСН или БД не
   поднята: `main.py:57-61`; ищите `POSTGRES POOL STARTED` —
   если нет, DB недоступна.
3. **worker падает при старте Kafka producer'а** — Redpanda
   недоступна/не поднята: `kafka_events.py:17-22`.
4. **vad-client падает при старте** — не поднят storage worker
   (gRPC `worker:50051` недоступен): `client_vad_service.py:180`.
5. **Triton контейнеры** — нет GPU / NVIDIA Container Toolkit:
   `docker info | egrep -i nvidia` и `nvidia-smi`.

## 2. Kafka / Redpanda недоступна

```bash
docker compose logs redpanda
docker compose exec redpanda rpk cluster info
docker compose exec redpanda rpk topic list
# из хоста:
nc -vz localhost 9092; nc -vz localhost 19092; nc -vz localhost 9644
```

Частые причины:
- advertised EXTERNAL адрес не соответствует вашему хосту/IP
  (docker-compose.yml:71, `172.16.20.111`) — ПК-клиенты не смогут
  подключиться через `19092`.
- Firewall / Security-Group блокируют `9092/19092`.
- Контейнер-клиент не может дойти до `redpanda:9092` — разные
  network; проверьте `docker network inspect <project>_speech`.

## 3. PostgreSQL недоступен

```bash
docker compose logs postgres
docker compose exec postgres pg_isready -h postgres -U speech
docker compose exec postgres psql -U speech -d speech_db -c "SELECT 1;"
```

- `FATAL: password authentication failed` — mismatch
  `POSTGRES_USER/PASSWORD` в сервисе vs в `postgres`.
- `FATAL: database "speech_db" does not exist` — БД не создана;
  `depends_on` не ждёт готовности, сервис поднялся раньше.

## 4. MinIO / S3 недоступен

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:9000/minio/health/live
docker compose logs minio
docker compose exec minio mc alias set m http://localhost:9000 minioadmin minioadmin
docker compose exec minio mc ls m/                       # увидеть бакеты
docker compose exec minio mc ls -r m/audio-sessions/ 2>&1 | head
```

- `403` / `AccessDenied` — ключи не совпадают (compose
  `S3_ACCESS_KEY_ID` vs MinIO `MINIO_ROOT_USER`).
- `NoSuchBucket` (в логах `worker`) — бакет `audio-sessions`
  не создан — см. `docs/deployment.md` шаг 2.

## 5. Triton (VAD / ASR) недоступен

```bash
# VAD
docker compose logs vad
curl -s http://localhost:8001/v2/health/ready
# список моделей VAD:
docker compose exec vad tritonserver --model-repository /mnt/models 2>&1 &
curl -s http://localhost:8001/v2/repository/index

# ASR (из хоста не напрямую — проброса на 8001 нет; провереть из контейнера)
docker compose run --rm asr python - <<'PY'
import urllib.request, json
print(urllib.request.urlopen("http://triton-asr:8001/v2/health/ready").read())
print(json.dumps(json.loads(urllib.request.urlopen("http://triton-asr:8001/v2/repository/index").read()), indent=2))
PY
```

- VAD: модель `online_vad` не загружена — ищите `Failed to load model online_vad`
  в логах `vad`.
- ASR: модель `emformer_conformer_online_baseline_v1` не найдена —
  аналогично в логах `asr`.
- GPU OOM на длинных сессиях — посмотрите `nvidia-smi`,
  уменьшите `OFFLINE_WORKERS` (если LLM-шаг) или разбивайте сессии.

## 6. ASR не получает аудио

Путь, который надо пройти. Проверяйте по одному этапу:

1. PC-клиент шлёт: `docker compose logs vad-client | grep Session`
   и `docker compose logs worker | egrep "session started|session ended"`.
2. Чанки доехали и в S3: `worker` логи `queued_s3_upload` →
   `s3_upload_complete`.
3. События в Kafka: `docker compose exec redpanda rpk topic consume audio_events --num 5`
   (или Console 8080).
4. asr_worker скачал и распознал:
   `docker compose logs asr_worker | egrep "s3_read|ASR stream|ASR FINAL"`.
5. asr_worker публикует: Kafka topic `asr_transcripts`
   (см. шаг 3).
6. Поступление в Postgres: `SELECT * FROM transcripts ORDER BY created_at DESC;`

Если где-то «провал» — смотрите логи конкретного сервиса из предыдущей
по цепочке пукты.

## 7. Текст не появляется в PostgreSQL

- Проверьте `asr_worker` — есть ли `DB SAVE ...` в логе?
  `asr_worker/main.py:49`.
- Если есть, но в БД не видно — check соединения
  (`docker compose exec postgres psql -U speech -d speech_db -c "SELECT session_id FROM transcripts ORDER BY created_at DESC LIMIT 3;"`).
- Если `asr_worker` не публикует `asr_transcripts`, а Kafka-топik
  есть записи — `producer start` / `send_and_wait` сбой:
  `asr_worker/producer.py`; ищите `KafkaError` в логе `asr_worker`.
- Если `is_final=false` во всех строках — final-фраза не дошла:
  `asr_worker` «застрял» в `SessionBuffer` (чужие
  `chunk_id`, не получен `is_end` —
  `session_buffer.py:144-155`).

## 8. Classification не работает

Входные события — `asr_transcripts`.
Выходные — `classified_events` + `transcripts.dialog_type`.

```bash
docker compose logs classification-service | egrep "CLASSIFICATION|EMIT|label="
docker compose exec redpanda rpk topic consume classified_events --num 5
docker compose exec postgres psql -U speech -d speech_db -c \
  "SELECT session_id, dialog_type, is_final, created_at FROM transcripts ORDER BY created_at DESC LIMIT 5;"
```

Частые:
- `EMIT` нет вообще — ни в одном чанке не нашлось фразы из
  `MISSIONS` (порог `THRESHOLD=1`). Классификатор считает
  гистограмму на каждом чанке, а не только на final
  (`classifier.py:31-50`; `service.py:168-192`).
- `EMIT` есть, но `label` — «пустая» миссия (`other`, `help`, ...) —
  какая фраза попала видно в `matched_words` события
  `classified_events`; списки фраз — `classification/policy.py:8`
  и `phrase_matcher.py` (`find_phrases`).
- «Новый клиент» (`new_client_session`) не создаётся —
  `DialogSession.process` не увидел «привет/пока»
  (`dialog_sessions.py:28-51`).

## 9. Алерт не срабатывает

Путь: PC-клиент → gRPC → VAD → worker → Kafka(`audio_events`) →
asr_worker(Kafka(`asr_transcripts`)) → alert_service.

По одному этап:

1. В `asr_transcripts` есть финальная фраза?
   `rpk topic consume asr_transcripts --num 10`.
2. `alert_service` её прочитал?
   `docker compose logs alert-service | egrep "ALERT|PURCHASE|SALESPERSON"`
   (строки в коде: `alerts_service.py:241-244`, `285-288`,
   `342-345`).
3. Если нет — проверьте `TRIGGER_PHRASES` / `BUY_PHRASES` /
   `SALESPERSON_PHRASES` в `alert_service/config.py` и
   `MAX_GAP` (`config.py:79`): возможно, фраза «не совпала» из-за
    стемминга (snowball, russ) — `text_matcher.py:1-15`.
 4. Уведомление ушло? — канал задаётся `NOTIFICATION_CHANNEL`
    (telegram|max, см. `notifications.py`) и креденшелы в `.env`.
    При отправке в MAX: `max_bot.py` шлёт `POST platform-api2.max.ru/messages`
    с токеном в заголовке `Authorization`. Ошибки —
    `logger.exception("notification send error")` в `alerts_service.py:`
    HTTP 401 = неверный/отозванный токен; 4xx/5xx = см. коды в dev.max.ru;
    бот должен быть добавлен в целевой чат/канал.

## 10. Смена продавца не работает

1. Продавец произнёс «имя консультанта + ФИО + пауза» — в
   `asr_transcripts` есть финальная строка с этой фразой?
2. `alert_service` — есть `👤 SALESPERSON ...`?
   (`alerts_service.py:342`)
3. PC-клиент получил `salesperson_changes` — есть 👤-строка в
   `client.log` / в GUI? (`windows_autorun/client.py:809-822`).
4. Новая сессия с новым `WORKER_NAME` — в логе `vad-client`
   `session started` / `worker` `session started`?
5. Postgres: `seller_id` обновлён?
   (`alert_service/alerts_service.py:147-161`, `save_salesperson_change`).

Частые причины:
- Пауза коротка — `pending_salesperson` (строка 60) не сбрасывается,
  т.к. `is_final` ещё не пришёл: `alerts_service.py:307-312`.
- Имя написано не по формату «имя фамилия» (два слова) —
  `alerts_service.py:314-320`.

## 11. Отчёт не сгенерирован

- `docker compose logs daily_stats_scheduler` — ищите `RAW REPORT:`,
  `OFFLINE ANALYSIS:`, `PIPELINE:`.
- Если `RAW REPORT: FAILED` — Postgres недоступен или пуст за дату:
  проверьте данные и `--date`.
- Если `OFFLINE ANALYSIS: FAILED` — Ollama недоступно
  (`OLLAMA_HOST`) или модель отсутствует:
  `curl http://<ollama-host>/api/tags`.
- `curl "http://localhost:8000/reports/daily?report_date=..."` — 404
  = файла нет в `./reports`.

## 12. PC-клиент не работает

- `windows_autorun/logs/client.log`:
  - `Microphone is unavailable` / `No input audio devices` —
    проблема с аудио-устройством:
    `python windows_autorun/list_devices.py`.
  - `gRPC error` — VAD gateway недоступен (`SERVER_IP:6000`).
  - `Kafka connection/coordinator error` — Redpanda EXTERNAL
    `SERVER_IP:19092` недоступна.
  - `Another Audio Analytics client is already running` — pid-lock
    `logs/client.lock`.
- Heartbeat: `ls -la windows_autorun/logs/client_heartbeat` —
  если файл старый — клиент «завис», наручен watchdog.
- Установить/переустановить: `install.bat`, `stop.bat`,
  `uninstall.bat` (см. `windows_autorun/README.md`).

## 13. Быстрый «светофор» проверки

```bash
echo "=== containers ==="; docker compose ps --format "table {{.Name}}\t{{.Status}}"
echo "=== kafka ==="; docker compose exec redpanda rpk topic list 2>/dev/null
echo "=== minio ==="; curl -s -o /dev/null -w "%{http_code}\n" http://localhost:9000/minio/health/live
echo "=== postgres ==="; docker compose exec postgres pg_isready -h postgres -U speech
echo "=== vad triton ==="; curl -s http://localhost:8001/v2/health/ready; echo
echo "=== stats api ==="; curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/docs
echo "=== redpanda console ==="; curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/
```

Все `200` / `OK` = базовый стержень стоит; дальше смотрите
smoke-тест (`docs/deployment.md` раздел 5).
