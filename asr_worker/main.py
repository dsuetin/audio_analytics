import asyncio
import logging
import sys
import time

from asr_worker.consumer import KafkaConsumerWrapper
from asr_worker.s3_client import S3Client
from asr_worker.session_buffer import SessionBuffer
from asr_worker.triton_client import TritonASRClient


logger = logging.getLogger(__name__)


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


configure_logging()


def wav_to_pcm(wav_bytes: bytes) -> bytes:
    import io
    import wave

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        return wf.readframes(wf.getnframes())


class ASRWorker:
    def __init__(self):
        self.s3 = S3Client(
            endpoint="http://minio:9000",
            key="minioadmin",
            secret="minioadmin",
            bucket="audio-sessions",
        )

        self.asr = TritonASRClient()
        self.buffer = SessionBuffer()

        self._running = True

    async def runner_loop(self):
        print("ASRWorker runner_loop started")

        while self._running:
            try:
                session_ids = list(self.buffer.buf.keys())

                for session_id in session_ids:
                    try:
                        print(f"runner session={session_id}")

                        # Если пришел последний кусок — сразу закрываем сессию
                        if await self.buffer.is_end_ready(session_id):

                            final = await self.buffer.pop_all(session_id)

                            if final:
                                print(
                                    f"final chunk session={session_id} "
                                    f"bytes={len(final)}"
                                )

                                text = await self.asr.send(
                                    session_id,
                                    final,
                                    is_last=True,
                                )

                                print(
                                    f"after final send session={session_id} "
                                    f"text={repr(text)}"
                                )

                            # cleanup
                            self.buffer.buf.pop(session_id, None)
                            self.buffer.locks.pop(session_id, None)

                            self.asr.started.discard(session_id)
                            self.asr.seq_map.pop(session_id, None)

                            print(
                                f"session closed session={session_id}"
                            )

                            continue
                        

                        if await self.buffer.is_stalled(
                            session_id,
                            timeout_sec=5,
                        ):
                            logger.error(
                                "chunk timeout session=%s "
                                "expected_chunk_missing",
                                session_id,
                            )

                            final = await self.buffer.pop_all(
                                session_id
                            )

                            if final:
                                await self.asr.send(
                                    session_id,
                                    final,
                                    is_last=True,
                                )

                            self.buffer.buf.pop(session_id, None)
                            self.buffer.locks.pop(session_id, None)

                            self.asr.started.discard(session_id)
                            self.asr.seq_map.pop(session_id, None)

                            print(
                                f"session force closed "
                                f"session={session_id}"
                            )

                            continue
                        # обычный streaming
                        chunk = await self.buffer.pop_if_ready(
                            session_id,
                            min_ms=160,  # как в benchmark
                        )

                        if chunk:
                            print(
                                f"before send session={session_id} "
                                f"bytes={len(chunk)}"
                            )

                            text = await self.asr.send(
                                session_id,
                                chunk,
                                is_last=False,
                            )

                            print(
                                f"after send session={session_id} "
                                f"text={repr(text)}"
                            )

                    except Exception:
                        logger.exception(
                            "session processing failed "
                            f"session_id={session_id}"
                        )

            except Exception:
                logger.exception("runner loop failed")

            await asyncio.sleep(0.01)

            
    async def handle_event(self, event: dict):
        print("HANDLE START")
        if event.get("type") != "audio_chunk_saved":
            return

        session_id = event["session_id"]
        payload = event["payload"]

        s3_key = payload["s3_key"]
        is_end = payload.get("is_end", False)
        chunk_id = payload["chunk_id"]

        started = time.perf_counter()

        audio = self.s3.get_object(s3_key)
        pcm = wav_to_pcm(audio)

        await self.buffer.add(session_id, chunk_id, pcm, is_end=is_end)
    
        elapsed_ms = (time.perf_counter() - started) * 1000

        logger.info(
            "📥 s3_read session_id=%s chunk_id=%s is_end=%s bytes=%s duration_ms=%.2f",
            session_id,
            chunk_id,
            is_end,
            len(audio),
            elapsed_ms,
        )
        print("HANDLE END")

    async def stop(self):
        self._running = False


async def main():
    worker = ASRWorker()

    consumer = KafkaConsumerWrapper(
        bootstrap="redpanda:9092",
        topic="audio_events",
    )

    await consumer.start()

    runner_task = asyncio.create_task(worker.runner_loop())

    try:
        await consumer.run(worker.handle_event)
    finally:
        await worker.stop()
        runner_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())