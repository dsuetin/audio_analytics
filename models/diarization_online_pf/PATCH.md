# PATCH.md — `diarization_online_pf` vs `diarization_online`

## 0. Базовая версия и доказательство проверенности

Production-база (контейнер `server_triton-speech-segmentation`,
`/mnt/models/diarization_online/`; контрольные суммы — в `CHECKSUMS.md`):

| Файл | md5 (production) |
|---|---|
| `config.pbtxt` | `5e468ce7a0d191b7443c075b84f85325` |
| `1/model.py` | `32a39ee940c8077ec50cbf98048869f2` |
| `1/model_impl.py` | `062975f6a8c2b45488c79464c1ed2d1e` |
| `1/utils.py` | `846d1dedb5a183696c9e1ee2171dea26` |

Изменены ровно два файла: `1/model_impl.py` (563→705 строк) и
`1/utils.py` (595→603 строки). Полный diff: `patches/model_impl.diff`
(264 строки) и `patches/utils.diff` (8 строк). `1/model.py` — байт-в-байт
оригинал; `config.pbtxt` — оригинал, заменён только `name`.

Этот же код (md5 `83ac0723…` / `691da583…`) проверен:
- 2026-11-11 `reports/PARTIAL_FINALIZATION_STATE_PRESERVATION_REPORT.md` —
  **9/9** acceptance-проверок (запись 180.45 s, 74/74 запроса);
- 2026-11-12 `reports/VAD_SESSION_PARTIAL_DIARIZATION_INTEGRATION_REPORT.md` —
  **49/49** проверок (3 VAD-сессии, реальный worker-путь, PG read-back, FULL).

## 1. Как работала исходная модель

Triton Python-модель: backend `python`, `sequence_batching` (decoupled).
Одна sequence = один магазин+день, идентификатор `correlation_id`.
Каждый входящий чанк — INT16 16 kHz (типово 3000 мс), VAD-marks, `chunk_id`:

- embeddings считаются внутренним вызовом модели `embedder`
  (`model_impl.py:368` в оригинале) и накапливаются в
  `self.sequence_id_embeddings[corrid][chunk_id]`;
- VAD-marks чанка — в `self.sequence_id_vad[corrid]` и
  `sequence_id_vad_mark_audiogram[corrid]`;
- результат выдавался **один раз — только при `end_signal`**
  (`if end_signal == True:`, ~строка 475): все накопленные embeddings →
  модель `clusterer` → RTTM → единственный ответ; сразу после — **полное
  освобождение состояния** (`del self.sequence_id_embeddings[corrid]`
  и связанные словари).

Следствие: промежуточных результатов не было, speaker ID были сырыми
номерами кластеров последней кластеризации.

## 2. Что изменено для PARTIAL FINALIZATION

`1/model_impl.py` (номера строк — в PF-версии):

- **L30-31** — константы `PF_MATCH_THRESHOLD = 0.75` (минимальный косинус
  для перематча raw-кластера на сохранённый reference) и
  `PF_REF_ALPHA = 0.5` (скользящее среднее при обновлении reference).
- **L34-39** `pf_normalize()` — L2-нормализация вектора (нуль → как есть).
- **L42-120** `pf_stable_speaker_mapping(raw_centroids, refs, threshold,
  alpha)` — глобальный жадный попарный маппинг raw-кластеров на сохранённые
  reference-центроиды: пары ранжируются по косинусу, каждая сторона —
  максимум в одной паре; возвращает `(stable_map, refs_upd, matches,
  new_ids)`.
- **L167** — новое поле `self.sequence_id_speaker_refs = {}`
  (`corrid → {stable_id → нормализованный центроид 256-d}`).
- **L203-204** — при `end_signal` refs последовательности удаляются.
- **L370-371** — новый параметр запроса `partial_finalization`
  ("true"/"false"); `do_finalize = end_signal or (partial_finalization
  and not end_signal)`.
- **L492, L523** — finalize-блок (вызов `clusterer` + RTTM) теперь
  выполняется при `do_finalize`, а не только при `end_signal`.
- **L585-640** — после finalize: центроид каждого raw-кластера считается,
  `pf_stable_speaker_mapping` переназначает сырые ID на стабильные,
  `sequence_id_speaker_refs[corrid]` обновляется (существующие —
  скользящее среднее alpha=0.5; новые — центроид кластера).
  RTTM в ответе возвращается уже с **стабильными** ID.

`1/utils.py`:
- **L136-146** `adjust_audiogram_marks()` — сдвиг `cumulative_offset`
  выполняется только для непустых чанков (`if chunk:`). Пустые сигнальные
  чанки (тишина + пустые vad_marks) больше не сдвигают mark-таймлайн —
  иначе PARTIAL-снапшот «засвечивался» бы в несуществующие ячейки.
  Для production-трафика (все чанки с marks) поведение не меняется.

Ничего другого не изменено: вызовы `embedder`/`clusterer`, формат ответа,
`config.pbtxt` (кроме `name`) — как в оригинале.

## 3. Как сохраняются embeddings и состояние кластеризации

При PARTIAL (`partial_finalization="true"`, без `sequence_end`):

- `sequence_id_embeddings[corrid]` (все чанки, все векторы) — **не
  удаляется**;
- `sequence_id_vad[corrid]`, `sequence_id_vad_mark_audiogram[corrid]` —
  **не удаляются**;
- вызов `clusterer` идёт, как и при FULL (по всем накопленным embeddings),
  но результат кластеризации никуда «не записывается» в состояние модели —
  используется только для формирования RTTM-снапшота ответа;
- `sequence_id_speaker_refs[corrid]` создаётся (при первом PARTIAL) и
  обновляется (см. §4).

При FULL (`sequence_end` → `end_signal`):

- `del self.sequence_id_embeddings[corrid]` + `del` словари vad
  (блок ~L196-210) и `del self.sequence_id_speaker_refs[seq_id]` (L203-204);
- т.е. **освобождение состояния происходит только при FULL, ровно один
  раз** (проверено: в 9/9-эксперименте state-RELEASED ровно 1 раз — после
  FULL; 49/49 — после FULL дня).

## 4. Как сохраняются стабильные speaker ID

Проблема: каждый finalize-вызов заново кластеризует все embeddings, и сырые
номера кластеров каждый раз нумеруются с 0 — говорящий, получивший ID `1`
на предыдущем PARTIAL, мог стать `2` на следующем.

Решение — словарь `sequence_id_speaker_refs[corrid]`:
`stable_id → нормализованный центроид (256-d)`. После каждого finalize:

1. центроид каждого raw-кластера вычисляется по его embeddings и
   нормализуется (`pf_normalize`);
2. `pf_stable_speaker_mapping` — глобальная жадная 1:1-сводка raw-кластеров
   с сохранёнными references по косинусу (порог `0.75`);
3. совпавший raw-кластер **сохраняет** старый stable ID; reference
   обновляется скользящим средним
   `ref = normalize(0.5*ref + 0.5*centroid)` (следует за drift'ом голоса);
4. несовпавший raw-кластер получает **новый** stable ID
   (`max(existing)+1`) — «новый speaker → новый ID», без переиспользования
   чужих.

ID за день — монотонные и без пропусков: в integration-тесте (run3) —
`1..4` на трёх VAD-сессиях. Юнит-проверки логики —
`/tmp/opencode/test_pf_mapping.py` (5 групп, ALL PASSED).

## 5. Чем PARTIAL отличается от FULL

| | PARTIAL | FULL |
|---|---|---|
| сигнал | параметр `partial_finalization="true"`, `sequence_end=false` | `sequence_end=true` (параметр Triton-запроса), `partial_finalization="false"` |
| результат | полный накопленный RTTM-снапшот (все чанки до сих пор) | финальный RTTM за весь день |
| embeddings / vad-состояние | **сохраняются** | **освобождаются** |
| speaker refs | обновляются | освобождаются |
| частота | сколько угодно раз | ровно 1 (последний запрос дня) |

Сигнальный запрос PARTIAL/FULL — обычный чанк: 48000 samples тишины
(3000 мс), **пустые vad_marks**, очередной `chunk_id`. Пустые marks +
PF-guard в `utils.py` гарантируют, что сигнальный чанк не сдвигает
mark-таймлайн: модельная сетка остаётся `3000 мс × n_sent_chunks`.
(В worker: `_pf_build_vad_marks(0)` — пустой `SpeakerSegment`.)

## 6. Как устроен snapshot результата

Ответ — обычный `DiarisationResponse` модели (строка-сериализация proto,
`diarization_protocol/output.proto`):
`repeated SpeakerSegment { repeated VoiceActivityMark va_marks;
uint32 speaker_id; }`.

- `va_marks` — пары `VA_MARK_BEGIN`/`VA_MARK_END` с `offset_ms`,
  **относительно начала sequence** (начала дня/эпохи), а не чанка;
- `speaker_id` — **стабильный** ID (не сырой номер кластера);
- снапшот — **полное накопление, не дельта**: N-й PARTIAL содержит всё,
  что было в (N-1)-м, плюс новое; потребитель просто заменяет актуальный
  результат последним снапшотом (worker так и делает —
  `_pf_apply_snapshot` перезаписывает, не суммирует);
- после FULL структура та же — финальная версия;
- сегменты, пересекающие границу VAD-сессии, в дневном результате
  вырезаются (clip) по реальному аудио `model_start + audio_sent_ms` —
  «засвет» в тишину/падинг исключён (guard `utils.py`).

## 7. Когда и как освобождается состояние sequence

- **только при `end_signal` (FULL)**: блок ~L196-210 `model_impl.py` —
  `del` `sequence_id_embeddings`, `sequence_id_vad`,
  `sequence_id_vad_mark_audiogram`, `sequence_id_speaker_refs`;
- при PARTIAL ничего не удаляется;
- отдельного колбэка «sequence истекла по idle-timeout» у Python-модели
  нет: при простое дольше `max_sequence_idle_microseconds` (1e9 мкс =
  ~11.6 дня) состояние формально истекает на стороне Triton, а словарь
  модели остаётся. Поэтому потребитель (worker) при потере непрерывности
  **не продолжает старую sequence**: создаёт новую (новый
  `correlation_id` = `md5(date|store|epoch)[:8]`, epoch++), старая эпоха
  помечается `identity_lost` — «explicit, never silent continue».

## 8. Ограничения и особенности, выявленные при тестировании

1. **Paced-протокол обязателен**: ровно один запрос в полёте
   (chunk → ответ → следующий). Модель decoupled: каждый запрос завершается
   пустым финальным ответом (`enable_empty_final_response=True`); для
   аудио-ячеек payload отсутствует (ответ = только финал).
2. Sequence-контроль (`sequence_id`/`sequence_start`/`sequence_end`)
   передаётся в **параметрах запроса** (протокол tritonclient 2.74.0);
   сервер мапит `sequence_id` → `request.correlation_id()`.
3. Пустые `vad_marks` сигнального чанка: mark-таймлайн не должен
   сдвигаться (guard `utils.py` `if chunk:`). Нарушение — «засвет»
   снапшота за реальную границу аудио.
4. Порог косинуса 0.75 консервативен: при сильном отличии условий записи
   speaker может получить новый ID (лучше лишний ID, чем перепутанные
   говорящие).
5. `partial_finalization` по умолчанию `"false"` — production-протокол
   (единственный FULL в конце дня) остаётся неизменным; патч
   production-независим и включается только явной передачей параметра.
6. Директория модели самодостаточна: `diarization_protocol` (proto)
   предоставляет conda execution-environment внутри Triton-образа
   (`/opt/tritonserver/speech-segmentation-conda-diarization-env.tar.gz`),
   а не репозиторий — в `1/` он не дублируется.
