import logging

logger = logging.getLogger(__name__)

import asyncpg


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
        chunk_id: int | None,
        text: str,
        is_final: bool,
    ):
        if not self.pool:
            raise RuntimeError("DB not started")
        try:
            async with self.pool.acquire() as conn:
                print("DB SAVE", session_id, text[:50])
                await conn.execute(
                    """
                    INSERT INTO sessions
                    (session_id, chunk_id, recognition_text, is_final)
                    VALUES ($1, $2, $3, $4)
                    """,
                    session_id,
                    chunk_id,
                    text,
                    is_final,
                )
        except Exception as e:
            logger.exception("DB insert failed: %s", e)

    async def stop(self):
        if self.pool:
            await self.pool.close()