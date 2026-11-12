# CHECKSUMS.md — контрольные суммы `diarization_online_pf`

## PF-модель (эта директория, состояние на 2026-11-12)

| Файл | md5 | Строк |
|---|---|---|
| `config.pbtxt` | `290e311eedf40cade68fb4e6ceda5d31` | 84 |
| `1/model.py` | `32a39ee940c8077ec50cbf98048869f2` | 1 (без завершающего `\n`) |
| `1/model_impl.py` | `83ac0723865bf2238ab698fc8736a70e` | 705 |
| `1/utils.py` | `691da583da33d8ee46a7172ce55f0a87` | 603 |

Проверка:

```bash
cd models/diarization_online_pf
md5sum config.pbtxt 1/model.py 1/model_impl.py 1/utils.py
# или скриптом (проверяет и копию, подготовленную для Triton):
bash scripts/verify_checksums.sh /path/to/prepared/diarization_online_pf
```

## Production-база (НЕ изменялась)

Контейнер `server_triton-speech-segmentation`, `/mnt/models/diarization_online/`:

| Файл | md5 |
|---|---|
| `config.pbtxt` | `5e468ce7a0d191b7443c075b84f85325` |
| `1/model.py` | `32a39ee940c8077ec50cbf98048869f2` |
| `1/model_impl.py` | `062975f6a8c2b45488c79464c1ed2d1e` |
| `1/utils.py` | `846d1dedb5a183696c9e1ee2171dea26` |

Отношение к PF: `1/model.py` — идентичен; `config.pbtxt` — отличается
только `name: "diarization_online"` → `name: "diarization_online_pf"`;
`1/model_impl.py` и `1/utils.py` — патч (diff в `patches/`).

## Доказательство проверенности PF-кода

md5 `1/model_impl.py` = `83ac0723…` и `1/utils.py` = `691da583…` — тот
самый код, который прошёл:

- `reports/PARTIAL_FINALIZATION_STATE_PRESERVATION_REPORT.md` (2026-11-11):
  9/9 acceptance-проверок, 74/74 запроса, state RELEASED 1 раз (после FULL);
- `reports/VAD_SESSION_PARTIAL_DIARIZATION_INTEGRATION_REPORT.md`
  (2026-11-12): 49/49 проверок, 3 VAD-сессии, PG read-back, FULL.
