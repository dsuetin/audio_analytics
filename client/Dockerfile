FROM python:3.10-slim

WORKDIR /app

# системные зависимости (важно для numpy + grpc + potential audio tooling)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# зависимости python
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# код клиента
COPY client /app/client
# COPY generated /app/generated
COPY proto /app/proto

# RUN mkdir -p /app/generated && \
#     python -m grpc_tools.protoc \
#     -I=/app/proto \
#     --python_out=/app/generated \
#     --grpc_python_out=/app/generated \
#     /app/proto/audio.proto

RUN python -m grpc_tools.protoc \
    -I=/app/proto \
    --python_out=/app \
    --grpc_python_out=/app \
    /app/proto/audio.proto \
    /app/proto/bridge.proto

ENV PYTHONPATH=/app

# запускаем VAD gateway (docker-side service)
CMD ["python", "-m", "client.client_vad_service"]
