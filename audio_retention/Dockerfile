FROM python:3.11-slim

WORKDIR /app

# Install boto3
RUN pip install --no-cache-dir boto3

# Copy only the retention monitor script
COPY retention_monitor.py /app/retention_monitor.py

# Create directories for logs and locks
RUN mkdir -p /var/log/audio_retention /var/lib/audio_retention

# Set environment defaults
ENV AUDIO_RETENTION_DAYS=30
ENV TIMEZONE=Europe/Moscow
ENV DRY_RUN=true
ENV MODE=scheduled
ENV SCHEDULE_HOUR=3
ENV MAX_PARALLEL_DELETES=10
ENV LOG_FILE=/var/log/audio_retention/retention.log
ENV LOCK_FILE=/var/lib/audio_retention/retention.lock

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import boto3; c=boto3.client('s3', endpoint_url='http://minio:9000', aws_access_key_id='minioadmin', aws_secret_access_key='minioadmin'); c.list_buckets(); exit(0)" || exit 1

# Run the retention monitor
CMD ["python", "retention_monitor.py"]
