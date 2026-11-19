"""Ingestion worker: takes job messages from the queue and processes them, one at a time.

Run it with `make worker` (on the host) or as the `worker` service of docker compose.

A message is deleted only once its job is finished (succeeded or permanently failed). If
the worker crashes or hits a transient error (database or model down), the message becomes
visible again after the visibility timeout and is retried; after 3 receives the queue
moves it to the dead-letter queue. Being idempotent is what makes this simple: processing
a message twice is always safe.
"""

from __future__ import annotations

import asyncio
import signal
import sys

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.database.session import build_engine, build_session_factory
from app.ingestion.jobs import process_job
from app.ingestion.queue import JobQueue, QueueMessage, build_job_queue
from app.ingestion.storage import ObjectStorage, build_storage
from app.rag.embeddings import EmbeddingProvider, build_embedding_provider

log = get_logger(__name__)


async def handle_message(
    message: QueueMessage,
    *,
    queue: JobQueue,
    sessions: async_sessionmaker[AsyncSession],
    storage: ObjectStorage,
    embeddings: EmbeddingProvider,
) -> None:
    bound = log.bind(job_id=str(message.job_id), receive_count=message.receive_count)
    if message.job_id is None:
        bound.warning("worker.invalid_message")  # retrying cannot fix it
        await queue.delete(message)
        return
    try:
        async with sessions() as session:
            status = await process_job(session, storage, embeddings, message.job_id)
    except Exception:
        bound.exception("worker.job_will_be_retried")  # the message stays in the queue
        return
    bound.info("worker.job_finished", status=status or "nothing to do")
    await queue.delete(message)


async def run_worker(settings: Settings, stop: asyncio.Event) -> None:
    engine = build_engine(settings.database_url)
    sessions = build_session_factory(engine)
    queue = build_job_queue(settings)
    storage = build_storage(settings)
    embeddings = build_embedding_provider(settings)
    log.info("worker.started", queue=settings.ingestion_queue_name, model=embeddings.model_id)
    try:
        while not stop.is_set():
            for message in await queue.receive():  # waits up to 10 s for a message
                await handle_message(
                    message, queue=queue, sessions=sessions, storage=storage, embeddings=embeddings
                )
    finally:
        await engine.dispose()
        log.info("worker.stopped")


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    stop = asyncio.Event()
    if sys.platform != "win32":  # on Windows, Ctrl+C interrupts directly
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            # Finish the current message, then stop (docker compose stop sends SIGTERM).
            loop.add_signal_handler(signum, stop.set)
    await run_worker(settings, stop)


if __name__ == "__main__":
    # psycopg's async mode cannot run on Windows' default ProactorEventLoop (ADR-0002).
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(main(), loop_factory=loop_factory)
