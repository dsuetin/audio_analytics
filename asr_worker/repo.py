import json
import logging
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

import asyncpg

# Moscow timezone (UTC+3) - same as VAD session_id format
MOSCOW_TZ = ZoneInfo('Europe/Moscow')


def parse_session_id(session_id: str):
    parts = session_id.split("-")

    if len(parts) >= 8:
        store_id = parts[2]

        uuid_str = "-".join(parts[-5:])
        UUID(uuid_str)  # проверка, что UUID корректный

        seller_id = "-".join(parts[3:-5])
    else:
        store_id = parts[0] if parts else ""
        seller_id = "-".join(parts[1:])

    return {
        "store_id": store_id,
        "seller_id": seller_id,
    }


def hhmmss_to_unix_ms(date_str: str, time_str: str, tz: ZoneInfo = MOSCOW_TZ) -> int:
    """
    Convert date (YYYYmmdd) and time (HHMMSS) from session_id to Unix timestamp (ms).
    
    Args:
        date_str: Date in format YYYYmmdd (e.g., "20261020")
        time_str: Time in format HHMMSS (e.g., "100000")
        tz: Timezone (default: Europe/Moscow = UTC+3)
    
    Returns:
        Unix timestamp in milliseconds
    """
    year = int(date_str[0:4])
    month = int(date_str[4:6])
    day = int(date_str[6:8])
    hour = int(time_str[0:2])
    minute = int(time_str[2:4])
    second = int(time_str[4:6])
    
    dt = datetime(year, month, day, hour, minute, second, tzinfo=tz)
    return int(dt.timestamp() * 1000)


def get_vad_session_start_ms(session_id: str) -> int | None:
    """
    Extract VAD session start time from session_id.
    
    Format: YYYYmmdd-HHMMSS-STORE_ID-SELLER_ID-UUID
    
    Returns:
        Unix timestamp in milliseconds, or None if parsing fails
    """
    try:
        parts = session_id.split('-')
        if len(parts) >= 2:
            date_str = parts[0]  # YYYYmmdd
            time_str = parts[1]  # HHMMSS
            return hhmmss_to_unix_ms(date_str, time_str)
    except (ValueError, IndexError) as e:
        logger.warning("Failed to parse session_id %s: %s", session_id, e)
    return None


def add_absolute_timestamps_to_words(words: list[dict], session_id: str) -> list[dict]:
    """
    Add absolute_start_ms and absolute_end_ms to each word.
    
    Formula: absolute_time = vad_session_start + word_relative_time
    
    Args:
        words: List of word dictionaries with start_time_ms and end_time_ms
        session_id: VAD session ID containing start time
    
    Returns:
        List of word dictionaries with added absolute_start_ms and absolute_end_ms
    """
    vad_session_start = get_vad_session_start_ms(session_id)
    
    if vad_session_start is None:
        logger.warning("Could not determine VAD session start time for %s", session_id)
        # Return words without absolute timestamps
        return words
    
    words_with_absolute = []
    for word in words:
        word_copy = word.copy()
        
        # Calculate absolute timestamps
        # Formula: absolute = vad_session_start + relative (NO additional offset)
        if "start_time_ms" in word:
            word_copy["absolute_start_ms"] = vad_session_start + word["start_time_ms"]
        
        if "end_time_ms" in word:
            word_copy["absolute_end_ms"] = vad_session_start + word["end_time_ms"]
        
        words_with_absolute.append(word_copy)
    
    return words_with_absolute
class TranscriptRepository:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None



    # async def start(self):
    #     self.pool = await asyncpg.create_pool(self.dsn)
    async def start(self):
        self.pool = await asyncpg.create_pool(self.dsn)
        logger.info("POSTGRES POOL STARTED")

    async def save(
        self,
        session_id: str,
        text: str,
        is_final: bool,
        words: list[dict] | None = None,
    ):
        if not self.pool:
            raise RuntimeError("DB not started")
        meta = parse_session_id(session_id)
        try:
            async with self.pool.acquire() as conn:
                # Add absolute timestamps to words if present
                if words:
                    words_with_absolute = add_absolute_timestamps_to_words(words, session_id)
                    words_json = json.dumps(words_with_absolute, ensure_ascii=False)
                else:
                    words_json = None
                
                await conn.execute(
                    """
                    INSERT INTO transcripts (
                        session_id,
                        store_id,
                        seller_id,
                        recognition_text,
                        is_final,
                        words
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (session_id)
                    DO UPDATE
                    SET
                        recognition_text = EXCLUDED.recognition_text,
                        is_final = EXCLUDED.is_final,
                        words = EXCLUDED.words;
                    """,
                    session_id,
                    meta["store_id"],
                    meta["seller_id"],
                    text,
                    is_final,
                    words_json,
                )
        except Exception as e:
            logger.exception("DB insert failed: %s", e)

    async def stop(self):
        if self.pool:
            await self.pool.close()
