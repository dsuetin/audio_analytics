from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    s3_endpoint_url: str = os.getenv("S3_ENDPOINT_URL", "http://localhost:9000")
    s3_access_key_id: str = os.getenv("S3_ACCESS_KEY_ID", "minioadmin")
    s3_secret_access_key: str = os.getenv("S3_SECRET_ACCESS_KEY", "minioadmin")
    s3_bucket: str = os.getenv("S3_BUCKET", "audio-sessions")

    kafka_bootstrap_servers: str = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    kafka_events_topic: str = os.getenv("KAFKA_EVENTS_TOPIC", "audio.events")

    grpc_host: str = os.getenv("GRPC_HOST", "0.0.0.0")
    grpc_port: int = int(os.getenv("GRPC_PORT", "50051"))

    min_part_size_bytes: int = int(os.getenv("MIN_PART_SIZE_BYTES", str(5 * 1024 * 1024)))
    max_buffer_bytes: int = int(os.getenv("MAX_BUFFER_BYTES", str(64 * 1024 * 1024)))
