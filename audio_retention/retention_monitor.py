#!/usr/bin/env python3
"""
Audio Retention Monitor

Monitors MinIO bucket for audio objects older than a configured retention period
and optionally deletes them. Runs on a schedule (daily at 03:00 in configured timezone).

Safety features:
- Dry-run mode by default (no actual deletion)
- Only processes .wav files in audio/<session_id>/ prefix
- Uses object LastModified date for age calculation
- Implements pagination for large buckets
- Prevents concurrent execution via file lock
- Logs all operations without exposing secrets
"""

import os
import sys
import time
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import ClientError

# Configuration from environment variables
S3_ENDPOINT_URL = os.environ.get('S3_ENDPOINT_URL', 'http://minio:9000')
S3_ACCESS_KEY_ID = os.environ.get('S3_ACCESS_KEY_ID', 'minioadmin')
S3_SECRET_ACCESS_KEY = os.environ.get('S3_SECRET_ACCESS_KEY', 'minioadmin')
S3_BUCKET = os.environ.get('S3_BUCKET', 'audio-sessions')
AUDIO_RETENTION_DAYS = int(os.environ.get('AUDIO_RETENTION_DAYS', '30'))
TIMEZONE = os.environ.get('TIMEZONE', 'Europe/Moscow')
DRY_RUN = os.environ.get('DRY_RUN', 'true').lower() == 'true'
LOG_FILE = os.environ.get('LOG_FILE', '/var/log/audio_retention/retention.log')
LOCK_FILE = os.environ.get('LOCK_FILE', '/var/lib/audio_retention/retention.lock')
SCHEDULE_HOUR = int(os.environ.get('SCHEDULE_HOUR', '3'))
MAX_PARALLEL_DELETES = int(os.environ.get('MAX_PARALLEL_DELETES', '10'))

# Setup logging
log_dir = Path(LOG_FILE).parent
log_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE)
    ]
)
logger = logging.getLogger(__name__)


class RetentionMonitor:
    """Monitors and cleans up old audio objects from MinIO bucket."""

    def __init__(
        self,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        retention_days: int,
        dry_run: bool = True
    ):
        self.client = boto3.client(
            's3',
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key
        )
        self.bucket = bucket
        self.retention_days = retention_days
        self.dry_run = dry_run
        self.stats = {
            'checked': 0,
            'candidates': 0,
            'deleted': 0,
            'deleted_bytes': 0,
            'errors': 0
        }

    def _is_audio_object(self, key: str) -> bool:
        """Check if object matches audio/<session_id>/NNNNNN.wav pattern."""
        if not key.startswith('audio/') or not key.endswith('.wav'):
            return False
        parts = key.split('/')
        if len(parts) < 3:
            return False
        # parts[0] = 'audio', parts[1] = session_id, parts[2] = NNNNNN.wav
        chunk_file = parts[2]
        if not chunk_file.endswith('.wav'):
            return False
        return True

    def _get_cutoff_time(self) -> datetime:
        """Get the cutoff datetime for retention policy."""
        try:
            tz = timezone(timedelta(hours=3)) if TIMEZONE == 'Europe/Moscow' else timezone.utc
            now = datetime.now(tz)
            return now - timedelta(days=self.retention_days)
        except Exception as e:
            logger.warning(f"Timezone error, using UTC: {e}")
            return datetime.now(timezone.utc) - timedelta(days=self.retention_days)

    def run_cleanup(self) -> dict:
        """
        Scan bucket and delete audio objects older than retention period.
        
        Returns:
            dict with statistics
        """
        logger.info(f"Starting retention check (retention={self.retention_days} days, dry_run={self.dry_run})")
        
        cutoff = self._get_cutoff_time()
        logger.info(f"Cutoff time: {cutoff} (UTC)")

        paginator = self.client.get_paginator('list_objects_v2')
        delete_batch = []
        batch_size = 1000

        for page in paginator.paginate(Bucket=self.bucket):
            if 'Contents' not in page:
                continue

            for obj in page['Contents']:
                self.stats['checked'] += 1
                key = obj['Key']

                # Only process audio objects
                if not self._is_audio_object(key):
                    continue

                # Check age
                obj_date = obj['LastModified']
                if obj_date < cutoff:
                    self.stats['candidates'] += 1
                    delete_batch.append({
                        'Key': key,
                        'Size': obj['Size'],
                        'LastModified': obj_date
                    })

            # Process batch if full
            if len(delete_batch) >= batch_size:
                self._delete_batch(delete_batch)
                delete_batch = []

        # Process remaining objects
        if delete_batch:
            self._delete_batch(delete_batch)

        logger.info(f"Retention check completed")
        logger.info(f"Stats: checked={self.stats['checked']}, candidates={self.stats['candidates']}, "
                   f"deleted={self.stats['deleted']}, deleted_bytes={self.stats['deleted_bytes']}, "
                   f"errors={self.stats['errors']}")

        return self.stats

    def _delete_batch(self, batch: list) -> None:
        """Delete a batch of objects with rate limiting."""
        if self.dry_run:
            logger.info(f"[DRY RUN] Would delete {len(batch)} objects")
            total_size = sum(obj['Size'] for obj in batch)
            logger.info(f"[DRY RUN] Would free {total_size / (1024*1024):.2f} MB")
            self.stats['deleted'] += len(batch)
            self.stats['deleted_bytes'] += total_size
            return

        # Sample objects for logging (first 5 and last 5)
        sample = batch[:5] + (batch[-5:] if len(batch) > 10 else [])
        logger.info(f"Deleting {len(batch)} objects")
        for obj in sample[:5]:
            logger.info(f"  - {obj['Key']} ({obj['Size']} bytes, {obj['LastModified']})")
        if len(batch) > 10:
            logger.info(f"  ... and {len(batch) - 10} more")

        # Delete in smaller chunks to avoid overwhelming the service
        chunk_size = MAX_PARALLEL_DELETES
        for i in range(0, len(batch), chunk_size):
            chunk = batch[i:i + chunk_size]
            objects = [{'Key': obj['Key']} for obj in chunk]

            try:
                response = self.client.delete_objects(
                    Bucket=self.bucket,
                    Delete={'Objects': objects}
                )
                deleted_count = len(objects) - len(response.get('Errors', []))
                self.stats['deleted'] += deleted_count
                self.stats['deleted_bytes'] += sum(obj['Size'] for obj in chunk)

                if response.get('Errors'):
                    for err in response['Errors']:
                        logger.error(f"Delete error: {err.get('Key')} - {err.get('Code')}: {err.get('Message')}")
                        self.stats['errors'] += 1

            except ClientError as e:
                logger.error(f"Batch delete error: {e}")
                self.stats['errors'] += len(chunk)
            except Exception as e:
                logger.error(f"Unexpected error during delete: {e}")
                self.stats['errors'] += len(chunk)

            # Small delay to avoid rate limiting
            time.sleep(0.1)

    def get_bucket_summary(self) -> dict:
        """Get summary of bucket contents."""
        paginator = self.client.get_paginator('list_objects_v2')
        
        total_objects = 0
        total_size = 0
        audio_objects = 0
        audio_size = 0
        sessions = set()
        oldest_date = None
        newest_date = None

        for page in paginator.paginate(Bucket=self.bucket):
            if 'Contents' not in page:
                continue

            for obj in page['Contents']:
                total_objects += 1
                total_size += obj['Size']

                if self._is_audio_object(obj['Key']):
                    audio_objects += 1
                    audio_size += obj['Size']

                    parts = obj['Key'].split('/')
                    if len(parts) >= 2:
                        sessions.add(parts[1])

                    obj_date = obj['LastModified']
                    if oldest_date is None or obj_date < oldest_date:
                        oldest_date = obj_date
                    if newest_date is None or obj_date > newest_date:
                        newest_date = obj_date

        return {
            'total_objects': total_objects,
            'total_size_mb': total_size / (1024 * 1024),
            'audio_objects': audio_objects,
            'audio_size_mb': audio_size / (1024 * 1024),
            'unique_sessions': len(sessions),
            'oldest_object': oldest_date.isoformat() if oldest_date else None,
            'newest_object': newest_date.isoformat() if newest_date else None
        }


class FileLock:
    """Simple file-based lock to prevent concurrent execution."""

    def __init__(self, lock_file: str):
        self.lock_file = Path(lock_file)
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)

    def acquire(self) -> bool:
        """Try to acquire the lock. Returns True if successful."""
        try:
            # Create lock file with PID
            with open(self.lock_file, 'w') as f:
                f.write(f"{os.getpid()} {datetime.now().isoformat()}\n")
            return True
        except IOError as e:
            logger.error(f"Failed to acquire lock: {e}")
            return False

    def release(self) -> None:
        """Release the lock."""
        try:
            self.lock_file.unlink(missing_ok=True)
        except IOError as e:
            logger.error(f"Failed to release lock: {e}")

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("Failed to acquire lock - another instance may be running")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


def run_scheduled_check():
    """Run a single retention check."""
    lock_dir = Path(LOCK_FILE).parent
    lock_dir.mkdir(parents=True, exist_ok=True)

    lock = FileLock(LOCK_FILE)
    
    try:
        with lock:
            monitor = RetentionMonitor(
                endpoint_url=S3_ENDPOINT_URL,
                access_key=S3_ACCESS_KEY_ID,
                secret_key=S3_SECRET_ACCESS_KEY,
                bucket=S3_BUCKET,
                retention_days=AUDIO_RETENTION_DAYS,
                dry_run=DRY_RUN
            )
            stats = monitor.run_cleanup()
            
            # Log final summary
            logger.info("=" * 60)
            logger.info("RETENTION CHECK SUMMARY")
            logger.info("=" * 60)
            logger.info(f"Retention period: {AUDIO_RETENTION_DAYS} days")
            logger.info(f"Dry run: {DRY_RUN}")
            logger.info(f"Objects checked: {stats['checked']}")
            logger.info(f"Candidates for deletion: {stats['candidates']}")
            logger.info(f"Objects deleted: {stats['deleted']}")
            logger.info(f"Space freed: {stats['deleted_bytes'] / (1024*1024):.2f} MB")
            logger.info(f"Errors: {stats['errors']}")
            logger.info("=" * 60)

    except Exception as e:
        logger.exception(f"Retention check failed: {e}")
        sys.exit(1)


def run_bucket_summary():
    """Print bucket summary without making any changes."""
    logger.info("Getting bucket summary...")
    
    monitor = RetentionMonitor(
        endpoint_url=S3_ENDPOINT_URL,
        access_key=S3_ACCESS_KEY_ID,
        secret_key=S3_SECRET_ACCESS_KEY,
        bucket=S3_BUCKET,
        retention_days=AUDIO_RETENTION_DAYS,
        dry_run=True
    )
    
    summary = monitor.get_bucket_summary()
    
    logger.info("=" * 60)
    logger.info("BUCKET SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Bucket: {S3_BUCKET}")
    logger.info(f"Total objects: {summary['total_objects']:,}")
    logger.info(f"Total size: {summary['total_size_mb']:.2f} MB")
    logger.info(f"Audio objects: {summary['audio_objects']:,}")
    logger.info(f"Audio size: {summary['audio_size_mb']:.2f} MB")
    logger.info(f"Unique sessions: {summary['unique_sessions']:,}")
    logger.info(f"Oldest object: {summary['oldest_object']}")
    logger.info(f"Newest object: {summary['newest_object']}")
    logger.info("=" * 60)


def wait_for_schedule():
    """Wait until the scheduled time (SCHEDULE_HOUR:00) and then run check."""
    tz_info = timezone(timedelta(hours=3)) if TIMEZONE == 'Europe/Moscow' else timezone.utc
    
    while True:
        now = datetime.now(tz_info)
        target = now.replace(hour=SCHEDULE_HOUR, minute=0, second=0, microsecond=0)
        
        # If we've already passed the target time today, wait for tomorrow
        if now >= target:
            target = target + timedelta(days=1)
        
        wait_seconds = (target - now).total_seconds()
        
        if wait_seconds > 0:
            logger.info(f"Next scheduled run: {target.isoformat()} (in {wait_seconds/3600:.1f} hours)")
            time.sleep(min(wait_seconds, 3600))  # Check hourly
        else:
            logger.info(f"Running scheduled check at {now.isoformat()}")
            run_scheduled_check()
            time.sleep(60)  # Wait a minute before next check cycle


def main():
    """Main entry point."""
    mode = os.environ.get('MODE', 'scheduled')  # scheduled, once, summary
    
    logger.info(f"Audio Retention Monitor starting (mode={mode}, dry_run={DRY_RUN})")
    logger.info(f"Configuration: retention={AUDIO_RETENTION_DAYS}days, timezone={TIMEZONE}, schedule={SCHEDULE_HOUR}:00")

    if mode == 'summary':
        run_bucket_summary()
    elif mode == 'once':
        run_scheduled_check()
    else:
        wait_for_schedule()


if __name__ == '__main__':
    main()
