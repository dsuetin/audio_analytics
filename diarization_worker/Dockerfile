FROM python:3.10-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create diarization_protocol directory and generate protobuf
RUN mkdir -p diarization_protocol

COPY diarization_protocol/output.proto ./diarization_protocol/
RUN python -m grpc_tools.protoc -I./diarization_protocol --python_out=./diarization_protocol ./diarization_protocol/output.proto

# Copy application code
COPY . .

# Set Python path
ENV PYTHONPATH=/app

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Run streaming diarization worker
CMD ["python", "streaming_diarization_worker.py"]
