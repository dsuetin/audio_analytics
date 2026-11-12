# `diarization_online_pf` — версия `diarization_online` с PARTIAL FINALIZATION

Triton Python-модель (backend `python`, `sequence_batching`, decoupled) —
теневая копия production-модели `diarization_online` с поддержкой:

- **PARTIAL FINALIZATION** — промежуточный полный RTTM-снапшот накопленного
  результата **без разрушения** состояния sequence (embeddings, VAD-марки);
- **стабильных speaker ID** — идентификаторы говорящих не сбрасываются
  между PARTIAL-финализациями.

**Назначение:** тестирование и эксперименты (страница `stats_service`
`/diarization-test`, ручные прогоны). **НЕ включена в production-поток**:
production-воркер использует только `diarization_online`.

## Состав каталога

- `config.pbtxt` — production-конфиг, изменён только `name`;
- `1/model.py` — оригинал без изменений;
- `1/model_impl.py` — основная логика + PF-патч (563 → 705 строк);
- `1/utils.py` — + guard `if chunk:` в `adjust_audiogram_marks` (595 → 603);
- `patches/` — полные diff'ы против оригинала (`model_impl.diff`, `utils.diff`);
- `PATCH.md` — подробное описание патча (8 разделов: как работала модель,
  что изменено, сохранение состояния и ID, PARTIAL vs FULL, снапшот,
  освобождение состояния, ограничения);
- `CHECKSUMS.md` — контрольные суммы (PF и production-база) + команды проверки;
- `scripts/prepare_model_dir.sh` — готовая директория модели для Triton
  (копирование + проверка md5);
- `MOUNT.md` — подключение к Triton (docker cp / volume mount), проверка
  READY, обновление и откат.

## Протокол клиента (кратко)

Paced: один запрос в полёте (chunk → ответ → следующий).

Входы запроса: `input` INT16 `(1, N)`; `vad_marks` BYTES `(1, 1)` —
сериализованный proto `SpeakerSegment` (`diarization_protocol/output.proto`);
`chunk_id` UINT16 `(1, 1)`.

Параметры: `sample_rate=16000`, `window_fs=1.52`, `period_fs=0.7`,
`frame_shift=10`, `max_speaker=20`, `num_speaker=0`,
`use_audiogram_vad=True`, `partial_finalization="true"|"false"`.
Sequence-контроль — `sequence_id` / `sequence_start` / `sequence_end`
(+ `enable_empty_final_response=True`).

Сигнальные запросы PARTIAL/FULL — чанк тишины 3000 мс (48000 samples)
с **пустыми** vad_marks и очередным `chunk_id`:
PARTIAL — `partial_finalization="true"`, `sequence_end=false`;
FULL — `sequence_end=true` (состояние освобождается).

## Быстрая проверка каталога

```bash
bash scripts/prepare_model_dir.sh --verify /path/to/diarization_online_pf
```
