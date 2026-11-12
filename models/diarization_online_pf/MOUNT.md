# MOUNT.md — подключение `diarization_online_pf` к Triton

## Текущая архитектура

- Контейнер `server_triton-speech-segmentation` (image `vad_server:latest`,
  внешняя сборка): **volume mounts нет вообще** — model repository
  `/mnt/models` зашит в образ;
- `--model-control-mode=explicit` — модели грузятся явно
  (`--load-model` в `TRITON_PARAMETERS` + gRPC model-control API :8001);
- поэтому **персистентный** host-mount требует изменения
  `docker-compose.yml` и **recreate контейнера** (перезапуск production
  Triton) → отдельное согласование (вариант B ниже).

## Вариант A — `docker cp` + gRPC load (без перезапуска, для тестов)

Сейчас используется страницей `/diarization-test` и экспериментами:

```bash
bash models/diarization_online_pf/scripts/prepare_model_dir.sh /tmp/pf_repo
docker cp /tmp/pf_repo/diarization_online_pf \
    server_triton-speech-segmentation:/mnt/models/diarization_online_pf
# load + READY (gRPC :8001):
python3 scripts/triton_model_ctl.py load diarization_online_pf
python3 scripts/triton_model_ctl.py ready diarization_online_pf
```

Особенности:

- production-модель `diarization_online` **не затрагивается** (отдельное
  имя в repository);
- без перезапуска контейнера и влияния на production-поток;
- **не персистентно**: после рестарта контейнера runtime-загруженная
  модель исчезает (её нет в `TRITON_PARAMETERS`). Для тестовой страницы
  допустимо: backend проверяет READY перед запуском теста и возвращает
  понятную ошибку, а команда выше восстанавливает модель за секунды.

## Вариант B — персистентный volume mount (план, требует согласования)

Требуется recreate контейнера `server_triton-speech-segmentation` →
кратковременная остановка VAD и production-моделей. **Выполняется только
после явного согласования** (входит в план production-rollout,
`reports/VAD_SESSION_PARTIAL_DIARIZATION_INTEGRATION_REPORT.md` §11).

`docker-compose.yml`, сервис `vad:` — добавить volume (остальное без
изменений):

```yaml
    volumes:
      - ./models/diarization_online_pf:/mnt/models/diarization_online_pf:ro
```

Опционально — автозагрузка после рестарта: добавить в `TRITON_PARAMETERS`
строку `--load-model diarization_online_pf`.

Применение: `docker compose up -d vad` (recreate контейнера), затем —
команды проверки ниже. `:ro`: Triton каталог модели только читает,
изменения — только в git с последующим задеплоем.

## Проверки (общие для A и B)

```bash
# директория внутри контейнера:
docker exec server_triton-speech-segmentation \
    sh -c 'ls /mnt/models/diarization_online_pf/1/'
# контрольная сумма (ожидаемо 83ac0723865bf2238ab698fc8736a70e):
docker exec server_triton-speech-segmentation \
    sh -c 'md5sum /mnt/models/diarization_online_pf/1/model_impl.py'
# статус READY:
python3 scripts/triton_model_ctl.py ready diarization_online_pf
```

## Обновление PF-кода

1. правки `models/diarization_online_pf/1/*.py` в git;
2. `bash scripts/prepare_model_dir.sh --verify .` + обновить md5 в
   `CHECKSUMS.md` и `EXPECTED` скрипта;
3. вариант A: повторно `docker cp`, затем `unload` + `load`;
   вариант B: mount подхватит файлы сам — достаточно `unload` + `load`
   (recreate контейнера не нужен).

## Откат

- вариант A: `python3 scripts/triton_model_ctl.py unload \
  diarization_online_pf` +
  `docker exec server_triton-speech-segmentation \
  rm -rf /mnt/models/diarization_online_pf` — исходники в git не удаляются;
- вариант B: убрать строку volume из compose и `docker compose up -d vad`;
  каталог в репозитории остаётся.

В обоих вариантах production `diarization_online` не затрагивается.
