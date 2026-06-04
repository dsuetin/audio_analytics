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
