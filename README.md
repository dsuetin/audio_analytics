# Audio ingestion starter

This repository is the first step of the production pipeline:

Mic/VAD client -> gRPC ingestion worker -> S3 multipart upload -> Kafka event log

## What is included

- `proto/audio.proto` — streaming contract
- `proto/bridge.proto` — internal VAD bridge contract
- `storage_worker/` — gRPC service that writes audio to S3 using multipart upload
- `storage_worker/kafka_events.py` — Kafka event publisher for upload lifecycle events
- `client/example_client.py` — example sender for raw PCM chunks
- `docker-compose.yml` — local infrastructure for MinIO + Kafka + the worker

## Proto split

There are two separate protobuf contracts:

- `audio.proto` is the storage contract. It is what the worker understands and what ends up in S3.
- `bridge.proto` is the VAD-facing contract. It receives microphone chunks, runs VAD, and converts them into the storage contract.

They must stay separate because the VAD bridge is intentionally a different boundary from storage. If both files share the same package and message names, Python protobuf generation can clash.

## Important design choice

Kafka is used only for events:
- `session_started`
- `part_uploaded`
- `session_completed`
- `session_failed`

Kafka does **not** carry raw audio.

## Run order

1. Start infrastructure with Docker Compose
2. Build and run the worker
3. Connect your VAD client to the gRPC endpoint
4. When `BEGIN` happens, start a new `session_id`
5. Stream only the chunks that belong to speech
6. Send `is_end=true` on session end

## S3 layout

The worker uploads one object per session using multipart upload:

`audio/<session_id>.raw`

This avoids the object explosion problem that happens when each chunk is stored as a separate S3 object.

## Next step after this starter

Split the ingestion service into:
- a gRPC gateway
- a separate storage worker

For now, this starter keeps the storage worker as the main executable, so you can begin wiring the client immediately.


python -m grpc_tools.protoc \
  -I=proto \
  --python_out=generated \
  --grpc_python_out=generated \
  proto/audio.proto


CREATE TABLE transcripts (
    session_id TEXT PRIMARY KEY,
    store_id TEXT,
    client_id TEXT,
    seller_id TEXT,
    recognition_text TEXT,
    dialog_type TEXT,
    is_sale BOOLEAN NOT NULL DEFAULT FALSE,
    is_alarm_triggered BOOLEAN NOT NULL DEFAULT FALSE,
    is_final BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

## Daily Excel report

The script `scripts/export_daily_transcript_report.py` exports one day of rows from `transcripts` into a readable `.xlsx` workbook and a matching `.pdf` report.

It creates these sheets:

- `Overview` with totals and report metadata
- `Records` sorted for store/seller review
- `Clients` sorted for client review
- `Store summary`
- `Seller summary`
- `Client summary`

Run it with either `POSTGRES_DSN` or the usual `POSTGRES_*` variables:

```bash
python3 scripts/export_daily_transcript_report.py --date 2026-07-12
```

By default it exports the previous day in `Europe/Moscow` and writes `transcript_report_<date>.xlsx` and `transcript_report_<date>.pdf`.

If you run PostgreSQL locally, set `POSTGRES_HOST=localhost`. If you run the script inside Docker, keep `POSTGRES_HOST=postgres`.

## Nightly stats service

The `stats_service/` container runs the same daily export automatically at `00:00` and saves the files into `/reports`.

Default behavior:

- `REPORT_TIMEZONE=Europe/Moscow`
- `REPORT_OUTPUT_DIR=reports` locally
- `REPORT_OUTPUT_DIR=/reports` in Docker
- `POSTGRES_HOST=postgres`

You can start it with Docker Compose together with the rest of the stack.

To run it on demand and exit immediately, pass `--once`. You can also force a specific date with `--date YYYY-MM-DD`.

If you run it locally and your shell has `POSTGRES_HOST=postgres` from Docker, pass `--postgres-host localhost` explicitly.


DELETE FROM transcripts
WHERE created_at::date = CURRENT_DATE;