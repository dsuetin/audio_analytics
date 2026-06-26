import logging
from uuid import UUID
logger = logging.getLogger(__name__)

import asyncpg

def parse_session_id(session_id: str):
    parts = session_id.split("-")

    date = parts[0]
    time = parts[1]
    store_id = parts[2]

    uuid_str = "-".join(parts[-5:])
    UUID(uuid_str)  # проверка, что UUID корректный

    seller_id = "-".join(parts[3:-5])

    return {
        "store_id": store_id,
        "seller_id": seller_id,
    }
class TranscriptRepository:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None



    # async def start(self):
    #     self.pool = await asyncpg.create_pool(self.dsn)
    async def start(self):
        self.pool = await asyncpg.create_pool(self.dsn)
        print("POSTGRES POOL STARTED")

    async def save(
        self,
        session_id: str,
        text: str,
        is_final: bool,
    ):
        if not self.pool:
            raise RuntimeError("DB not started")
        meta = parse_session_id(session_id)
        try:
            async with self.pool.acquire() as conn:
                print("DB SAVE", session_id, text[:50])
                await conn.execute(
                    """
                    INSERT INTO transcripts (
                        session_id,
                        store_id,
                        seller_id,
                        recognition_text,
                        is_final
                    )
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (session_id)
                    DO UPDATE
                    SET
                        recognition_text = EXCLUDED.recognition_text,
                        is_final = EXCLUDED.is_final;
                    """,
                    session_id,
                    meta["store_id"],
                    meta["seller_id"],
                    text,
                    is_final,
                )
        except Exception as e:
            logger.exception("DB insert failed: %s", e)

    async def stop(self):
        if self.pool:
            await self.pool.close()