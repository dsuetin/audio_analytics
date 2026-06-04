# FROM python:3.11-slim
FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends     gcc     && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY proto /app/proto
COPY storage_worker /app/storage_worker

# RUN python -m grpc_tools.protoc     -I/app/proto     --python_out=/app/storage_worker     --grpc_python_out=/app/storage_worker     /app/proto/audio.proto
RUN python -m grpc_tools.protoc \
    -I=/app/proto \
    --python_out=/app \
    --grpc_python_out=/app \
    /app/proto/audio.proto

ENV PYTHONPATH=/app

CMD ["python", "-m", "storage_worker.server"]
