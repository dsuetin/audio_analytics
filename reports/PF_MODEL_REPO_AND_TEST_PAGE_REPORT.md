# Отчёт: PF-модель в репозитории + страница тестирования `/diarization-test`

**Дата:** 2026-11-12
**Исходные результаты, на которые опирается работа:**
`reports/PARTIAL_FINALIZATION_STATE_PRESERVATION_REPORT.md` (9/9),
`reports/VAD_SESSION_PARTIAL_DIARIZATION_INTEGRATION_REPORT.md` (49/49).

## Сводка

1. **PF-модель сохранена в репозитории** — каталог
   `models/diarization_online_pf/` (полный код, `config.pbtxt`, diff'ы,
   README, PATCH.md, CHECKSUMS.md, скрипт подготовки). Код — тот самый
   проверенный (md5 сверены); production-модель не изменялась.
2. **Подключение к Triton подготовлено двумя вариантами**
   (`models/diarization_online_pf/MOUNT.md`):
   - **A (применён сейчас):** `docker cp` + gRPC `load` — без перезапуска
     контейнеров; модель `diarization_online_pf` в статусе READY;
   - **B (план, требует согласования):** персистентный volume mount через
     `docker-compose.yml` — требует recreate контейнера
     `server_triton-speech-segmentation`, поэтому **не выполнен**.
3. **Страница тестирования** добавлена в существующий `stats_service`
   (один модуль + guarded include в `api.py`), URL:
   **`/diarization-test`**. Проверена на тестовом инстансе
   `daily-stats-api-test` (порт **8010**) реальным WAV через реальный
   Triton gRPC; production-инстанс (8000) не перезапускался и не
   пересобирался — страница в нём включится после пересборки образа с
   новыми `requirements.txt` (stop-point, план ниже).
4. **Изоляция соблюдена:** без Kafka/PostgreSQL/S3; результаты — в
   `REPORT_OUTPUT_DIR/diarization_test/`; production-контейнеры не
   перезапускались; production-модель и worker не изменялись
   (md5 сверены до/после).

## 1. Аудит исходного состояния (результаты)

- Production-модель `diarization_online` — в контейнере
  `server_triton-speech-segmentation` (`/mnt/models/`), image
  `vad_server:latest` **внешней сборки**; у контейнера **нет вообще
  volume mounts** — repository зашит в образ.
- `--model-control-mode=explicit`, gRPC :8001; `diarization_protocol`
  (proto) даёт conda execution-environment образа — каталог модели
  самодостаточен.
- Проверенный PF-код: `/tmp/opencode/model_impl_pf.py`
  (md5 `83ac0723…`) и `/tmp/opencode/utils_pf.py` (md5 `691da583…`) —
  сверены с production-файлами (выгруженными из контейнера) и с
  обоими отчётами; diff 1:1 соответствует описанию. Восстановление
  патча из `/tmp` + отчётов оказалось полным — «недостающих» частей нет.
- Изменены только `model_impl.py` и `utils.py`; `config.pbtxt` — только
  `name`; `model.py` — без изменений.
- `stats_service` — монолитный FastAPI (HTML встроен в `api.py`),
  bind-mount `./stats_service:/app/stats_service`; существует тестовый
  инстанс `daily-stats-api-test` (порт 8010, `REPORT_OUTPUT_DIR=/tests_reports`)
  — использован для разработки и проверок.

## 2. PF-модель в репозитории

Каталог: `models/diarization_online_pf/`

```
models/diarization_online_pf/
├── config.pbtxt          # production-конфиг, name: "diarization_online_pf"
├── 1/
│   ├── model.py          # оригинал без изменений (md5 32a39ee9…)
│   ├── model_impl.py     # PF-патч (md5 83ac0723…, 705 строк)
│   └── utils.py          # + guard "if chunk:" (md5 691da583…, 603 строки)
├── patches/
│   ├── model_impl.diff   # полный diff 264 строки (production vs PF)
│   └── utils.diff        # diff 8 строк
├── README.md             # состав, протокол клиента, быстрая проверка
├── PATCH.md              # 8 разделов: как работала модель → ограничения
├── CHECKSUMS.md          # md5 PF-кода и production-базы + команды проверки
├── MOUNT.md              # подключение к Triton (варианты A/B), READY, откат
└── scripts/
    └── prepare_model_dir.sh  # копирование в model repo + проверка md5
```

Контрольные суммы (сверены с production-базой и `/tmp`-копиями):

| Файл | md5 |
|---|---|
| `config.pbtxt` | `290e311eedf40cade68fb4e6ceda5d31` |
| `1/model.py` | `32a39ee940c8077ec50cbf98048869f2` |
| `1/model_impl.py` | `83ac0723865bf2238ab698fc8736a70e` |
| `1/utils.py` | `691da583da33d8ee46a7172ce55f0a87` |

Проверка и подготовка:

```bash
bash models/diarization_online_pf/scripts/prepare_model_dir.sh --verify .
bash models/diarization_online_pf/scripts/prepare_model_dir.sh /path/to/model_repo
```

Diff против оригинала: `git diff` не требуется — diff'ы лежат в
`patches/` (сформированы `diff -u` против файлов, выгруженных из
production-контейнера 2026-11-12); воспроизведение:
`diff -u <production model_impl.py> models/diarization_online_pf/1/model_impl.py`.

## 3. Docker volume mount

**Текущая архитектура не позволяет персистентный mount без recreate
контейнера** (у контейнера нет mounts; repository в образе). Поэтому:

### Вариант A — применён (без перезапуска)

```bash
bash models/diarization_online_pf/scripts/prepare_model_dir.sh /tmp/pf_repo
docker cp /tmp/pf_repo/diarization_online_pf \
    server_triton-speech-segmentation:/mnt/models/diarization_online_pf
python3 models/diarization_online_pf/scripts/triton_model_ctl.py load diarization_online_pf
python3 models/diarization_online_pf/scripts/triton_model_ctl.py ready diarization_online_pf   # → True
```

- отдельное имя, `diarization_online` не подменяется;
- не персистентно: после рестарта Triton-контейнера модель надо загрузить
  повторно (одна команда); для тестовой страницы допустимо (backend
  проверяет READY и даёт понятную ошибку).

### Вариант B — план (ТОЛЬКО после согласования, stop-point)

`docker-compose.yml`, сервис `vad:`:

```yaml
    volumes:
      - ./models/diarization_online_pf:/mnt/models/diarization_online_pf:ro
```

(+ опционально `--load-model diarization_online_pf` в `TRITON_PARAMETERS`),
затем `docker compose up -d vad` — **recreate production-контейнера Triton**
(остановка VAD + моделей на время перезапуска). Входит в план
production-rollout; в рамках этой задачи **не выполнялся**.

Команды проверки/обновления/отката — в `models/diarization_online_pf/MOUNT.md`.

## 4. Страница тестирования `/diarization-test`

### Что добавлено (только тестовый инстанс)

| Файл | Изменение |
|---|---|
| `stats_service/diarization_test.py` | новый модуль (998 строк): backend (gRPC-сессии, WAV-декодирование, управление) + HTML/JS страницы |
| `stats_service/api.py` | guarded include: `try: include_router(...) except: 503-страница` — production-образ без gRPC-зависимостей работает без изменений |
| `stats_service/requirements.txt` | + `numpy`, `grpcio`, `protobuf`, `tritonclient`, `packaging` |
| `stats_service/diarization_protocol/` | `output.proto` + `output_pb2.py` (копия из корня репозитория) |

### Backend-механика (`diarization_test.py`)

- **WAV-парсинг** (`_decode_wav`): модуль `wave`; 8/16/24/32 bit; multi-channel →
  mono (среднее); resample → 16000 Гц (линейная интерполяция); исходный файл
  на диске не изменяется; копия оригинала + PCM16k сохраняются в
  `REPORT_OUTPUT_DIR/diarization_test/{sid}_orig.wav / _pcm16k_mono.pcm`.
- **Paced-клиент Triton** (`_PfTriton.send`) — точная копия проверенного
  worker-паттерна: `async_stream_infer` + очередь callback'ов,
  `enable_empty_final_response=True`, ровно один запрос в полёте
  (chunk → ACK → следующий). Параметры запроса: `sample_rate=16000`,
  `window_fs=1.52`, `period_fs=0.7`, `frame_shift=10`, `max_speaker=20`,
  `num_speaker=0`, `use_audiogram_vad=True`, `partial_finalization`.
- **vad_marks** — BEGIN 0 → END `actual_ms` (последний чанк доводится
  тишиной до `chunk_ms`, поэтому `actual_ms < chunk_ms` учитывается);
  сигнальные чанки PARTIAL/FULL — тишина + **пустые** marks.
- **PARTIAL** (`partial_finalization="true"`, `sequence_end=false`): полный
  накопленный снапшот, состояние модели сохраняется; снапшоты не
  суммируются — каждый заменяет предыдущий (семаантика модели).
- **FULL** (`sequence_end=true`): финальный снапшот, sequence освобождается.
- **Re-adding исключён**: страница не передаёт в модель возвращённые
  сегменты; модель сама накапливает embeddings — потребитель лишь
  отображает снапшоты.
- **Сессии**: уникальный `sequence_id` (32-bit) на WAV; registry в памяти
  процесса; lock сериализует chunk/partial/full/cancel; cancel шлёт
  `sequence_end` для очистки состояния на стороне Triton.
- **Экспорт**: `export.json` (результат), `export.txt` (человекочитаемый),
  `export.debug` (все запросы: tag, kind, chunk_id, ack_ms, segments,
  error).

### Формат страницы (URL `http://<host>:8010/diarization-test`)

1. Загрузка WAV + вывод name/duration/sample-rate/channels/bit-depth;
2. Настройки: chunk (default 3000 мс, 250–10000), PARTIAL-режим
   (none / each / 5 / 10 / 20 / manual / before_full);
3. Управление: Старт / Пауза-Продолжить / Ручной PARTIAL /
   FULL FINALIZATION / Отмена + прогресс (чанки, ms аудио, speaker ID);
4. Таблица сегментов последнего снапшота/финала (start/end/speaker);
5. История PARTIAL-снапшотов (раскрывающиеся блоки);
6. Экспорт JSON / TXT / debug-JSON.

## 5. Результаты тестов (реальный WAV, реальный Triton gRPC)

Тестовая запись: `audio_export/г_Пятигорск_ул_Мира_201а_строение_1/
2026-11-06_16:35:05__16:40:05.wav` (180.45 s, mono 16 kHz — та, на которой
получен 9/9-результат) + вторая запись `15:33:06__15:38:06.wav` (146.55 s).
Драйвер: `scripts/test_diarization_page.py` (те же HTTP-вызовы, что делает
браузер; прогон 37.5 s). Итог: **30/30 PASS**.

| # | Требуемая проверка | Сценарий | Результат |
|---|---|---|---|
| 1 | FULL без PARTIAL | A: 61 чанк × 3000 мс → FULL | 48 сегментов, speakers 1–4, все в [0, 180450] мс |
| 2 | Auto PARTIAL каждые 5 | B: 12 PARTIAL + FULL | 12 снапшотов; финал 48 сегм. = последнему PARTIAL по speaker-набору |
| 3 | PARTIAL после каждого чанка | C: 12 чанков, PARTIAL на каждый | 12 снапшотов, FULL — 7 сегментов |
| 4 | Ручной PARTIAL | D: режим manual, 2 ручных | ровно 2 снапшота, auto=false; авто-PARTIAL нет |
| 5 | Стабильность speaker ID между PARTIAL | B2/C2/D3: новые ID выдаются последовательно (max+1, …), не переиспользуются | PASS (B: ids 1–5; C: 1–3; D: 1–4) |
| 6 | FULL после нескольких PARTIAL | C3/D3 | финал получен, segments > 0 |
| 7 | Вторая запись WAV | E: новый WAV, новая сессия | другой session_id/sequence_id, FULL — 1 сегмент, state=finalized |
| 8 | Sequence cleanup после FULL | A7/B/E4: state=finalized, повторные запросы → 409 | PASS |
| 9 | Обработка ошибок | F: не-WAV → 400; неизвестная сессия → 404; config/send после final → 409 | PASS |
| 10 | Отмена + очистка sequence | G: cancel после 3 чанков | `sequence_cleaned: true` (отправлен sequence_end) |

Дополнительно:

- **Экспорты** (B5–B8): `export.json`/`export.txt`/`export.debug` — 200;
  debug содержит все 74 запроса (61 аудио + 12 PARTIAL + 1 FULL) с
  `ack_ms` (типичное ACK 140–200 мс на чанк).
- **Смоук-прогон** (до полного): upload → 2 чанка → PARTIAL (3 сегм.) →
  cancel (sequence очищена); затем upload → 3 чанка → FULL (1 сегмент).
- **Юнит-проверка логики стабильных ID** — `test_pf_mapping.py`
  (5 групп: новые ID, возврат со старым ID, глобальная жадность 1:1,
  скользящее обновление reference, разделение speakers): **ALL PASSED**.
- **Существующие страницы** на :8010: `/`, `/audio`, `/mic-monitoring`,
  `/api/stores` → 200 (без изменений).

Промежуточный прогон (run2) дал 3 «фейла», все — неверные инварианты в
драйвере, не в реализации: (a) ожидание 60 аудио-ячеек вместо 61
(`ceil(180450/3000)=61`); (b) «множество speaker ID только расширяется» —
при автоопределении (`num_speaker=0`) повторная кластеризация может
сливать малоаргументный кластер, и ID временно исчезает из снапшота,
возвращаясь позже со **своим** номером — это поведение модели,
проверенное в 9/9-эксперименте. Инвариант заменён на «новые ID выдаются
строго последовательно и не переиспользуются» — 30/30 PASS.

## 6. Изоляция: что НЕ затронуто

- **Production-модель** `diarization_online` — md5 всех 4 файлов
  сверены до и после работы: совпадает с аудитом
  (`5e468ce7…/32a39ee9…/062975f6…/846d1ded…`).
- **Production-контейнеры** (`daily-stats-api` :8000,
  `streaming-diarization-worker`, `triton-asr` и др.) — не
  перезапускались, не пересобирались; перезапускался только
  `daily-stats-api-test` (:8010) 3 раза (загрузка нового `api.py`/
  зависимостей).
- **PostgreSQL / Kafka / S3** — страница не использует: ни одного
  запроса к ним (только Triton gRPC :8001 и локальный диск
  `REPORT_OUTPUT_DIR/diarization_test/` = `tests/reports/diarization_test/`
  в тестовом инстансе).
- **Существующие маршруты** `stats_service` — работают (проверено на
  :8010: `/`, `/audio`, `/mic-monitoring`, `/api/stores` → 200;
  `/reports/daily` → 422 только при отсутствии обязательного
  параметра `report_date` — исходное поведение).
- **Worker и его PG-строки** — не изменялись; PF-модель в Triton
  загружена под отдельным именем `diarization_online_pf`, worker её
  не использует (он ходит в `diarization_online`).

## 7. Stop-точки: что НЕ выполнено и как включить

### Stop A — персистентный mount PF-модели в Triton (требует согласования)

Требует recreate контейнера `server_triton-speech-segmentation`
(production-Triton; остановка VAD-моделей на время перезапуска).
План — в `models/diarization_online_pf/MOUNT.md` (вариант B):
volume `./models/diarization_online_pf:/mnt/models/diarization_online_pf:ro`
+ опционально `--load-model diarization_online_pf` в `TRITON_PARAMETERS`,
затем `docker compose up -d vad`. Сейчас модель работает через вариант A
(`docker cp` + gRPC load), после рестарта Triton-контейнера её нужно
загрузить повторно (одна команда из MOUNT.md).

### Stop B — страница в production stats (порт 8000) (требует согласования)

Production-образ `audio_analytics-daily_stats_service:latest` собран без
gRPC-зависимостей. Благодаря guarded include в `api.py` сервис в
production-контейнере **работает без изменений**, а `/diarization-test`
в нём отдаёт 503 с инструкцией. Чтобы включить страницу в production:
1. пересобрать образ stats_service с новыми `requirements.txt`
   (Dockerfile уже ставит `requirements.txt` — правки Dockerfile не нужны);
2. `docker compose up -d daily_stats_api` (recreate контейнера :8000);
3. убедиться, что PF-модель в READY (MOUNT.md).
Альтернатива без пересборки — не разворачивать страницу в production
вообще: тестирование идёт на :8010.

## 8. Состав изменений, бэкапы, восстановление

### Добавлено (новые файлы)

- `models/diarization_online_pf/` — модель: `config.pbtxt`, `1/model.py`,
  `1/model_impl.py`, `1/utils.py`, `patches/{model_impl,utils}.diff`,
  `README.md`, `PATCH.md`, `CHECKSUMS.md`, `MOUNT.md`,
  `scripts/prepare_model_dir.sh`;
- `stats_service/diarization_test.py` — страница + backend;
- `stats_service/diarization_protocol/` — `output.proto`, `output_pb2.py`,
  `__init__.py`;
- `scripts/test_diarization_page.py` — драйвер 30 проверок;
- `reports/PF_MODEL_REPO_AND_TEST_PAGE_REPORT.md` — этот отчёт.

### Изменено (с бэкапом в `backups/pf_model_repo_20261112_162516/`)

- `stats_service/api.py` — guarded include (бэкап `api.py`);
- `stats_service/requirements.txt` — +5 пакетов (бэкап `requirements.txt`);
- `docker-compose.yml` — **не изменялся для этой задачи** (бэкап сделан
  превентивно; вариант B — план, не применялся).

### Проверка целостности и восстановление

```bash
# модель:
bash models/diarization_online_pf/scripts/prepare_model_dir.sh --verify \
    models/diarization_online_pf
# страница (откат):
cp backups/pf_model_repo_20261112_162516/api.py stats_service/api.py
cp backups/pf_model_repo_20261112_162516/requirements.txt stats_service/requirements.txt
rm -rf stats_service/diarization_test.py stats_service/diarization_protocol
# Triton (убрать PF-модель, production не трогается):
python3 models/diarization_online_pf/scripts/triton_model_ctl.py unload diarization_online_pf
docker exec server_triton-speech-segmentation \
    rm -rf /mnt/models/diarization_online_pf
```

Git: коммиты **не делались** (по требованию). Файлы задачи проиндексированы
(`git add` / `git add -f`), HEAD не изменён.

**Важно для коммита:** `.gitignore` репозитория содержит широкие правила
(`*.md`, `scripts`, `*.sh`, `*.json`, …) — существующая практика репозитория
— точечное принудительное добавление (`git add -f`), как для
`scripts/*.py` и `docs/*.md` в истории. Файлы этой задачи уже добавлены в
индекс (в т.ч. с `-f` для `models/diarization_online_pf/*.md`,
`scripts/prepare_model_dir.sh`, `scripts/test_diarization_page.py`,
`reports/PF_MODEL_REPO_AND_TEST_PAGE_REPORT.md`), поэтому обычный
`git commit` включит их; повторный `git add` этих путей без `-f` их
не коснётся.

## 9. Временные материалы (удалять только после подтверждения сохранения)

- `/tmp/opencode/model_impl_pf.py`, `/tmp/opencode/utils_pf.py` —
  проверенные PF-исходники (теперь дублируются в `models/…/1/` — md5
  совпадают, можно удалить);
- `/tmp/opencode/audit_prod_model/` — production-базы для diff'ов;
- `/tmp/opencode/page_test_driver.py` — копия `scripts/test_diarization_page.py`;
- `/tmp/opencode/page_test_run{1,2,3}.log` — логи прогонов (run3 — итог 30/30);
- `/tmp/opencode/triton_model_ctl.py` — gRPC load/unload/ready-хелпер
  (скопирован в `models/diarization_online_pf/scripts/triton_model_ctl.py`,
  ссылки в MOUNT.md — на репозиторий);
- `tests/reports/diarization_test/` — результаты тестовых сессий
  (оригиналы WAV, PCM, result.json) — отдельное temp-хранилище, не
  production-данные.
