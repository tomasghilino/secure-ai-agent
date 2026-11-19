"""Ingestion worker: takes job messages from the queue and processes them, one at a time.

Run it with `make worker` (on the host) or as the `worker` service of docker compose.

A message is deleted only once its job is finished (succeeded or permanently failed). If
the worker crashes or hits a transient error (database or model down), the message becomes
visible again after the visibility timeout and is retried; after 3 receives the queue
moves it to the dead-letter queue, and the job is marked failed. Being idempotent is what
makes this simple: processing a message twice is always safe.

Each job runs under its tenant (Row-Level Security). Every few minutes the worker also
sweeps jobs the normal flow left behind (see ``sweep_jobs``).
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.database.session import build_engine, build_session_factory
from app.database.tenancy import tenant_session
from app.ingestion.jobs import fail_job, process_job, sweep_jobs
from app.ingestion.queue import JobQueue, QueueMessage, build_job_queue
from app.ingestion.storage import ObjectStorage, build_storage
from app.rag.embeddings import EmbeddingProvider, build_embedding_provider

SWEEP_INTERVAL_SECONDS = 60.0

log = get_logger(__name__)


async def handle_message(
    message: QueueMessage,
    *,
    queue: JobQueue,
    sessions: async_sessionmaker[AsyncSession],
    storage: ObjectStorage,
    embeddings: EmbeddingProvider,
    max_receives: int = 3,
) -> None:
    job = message.job
    bound = log.bind(
        job_id=str(job.job_id) if job else None,
        tenant_id=job.tenant_id if job else None,
        receive_count=message.receive_count,
    )
    if job is None:
        bound.warning("worker.invalid_message")  # retrying cannot fix it
        await queue.delete(message)
        return
    try:
        async with tenant_session(sessions, job.tenant_id) as session:
            status = await process_job(session, storage, embeddings, job.job_id)
    except Exception as exc:
        if message.receive_count < max_receives:
            bound.exception("worker.job_will_be_retried")  # the message stays in the queue
            return
        # Last delivery: the queue moves the message to the dead-letter queue next time.
        bound.exception("worker.job_gave_up")
        async with tenant_session(sessions, job.tenant_id) as session:
            await fail_job(session, job.job_id, f"gave up after {max_receives} attempts: {exc}")
        return
    bound.info("worker.job_finished", status=status or "nothing to do")
    await queue.delete(message)


async def sweep(sessions: async_sessionmaker[AsyncSession], queue: JobQueue) -> None:
    try:
        async with sessions() as session:  # no tenant: the sweep looks across tenants
            requeued, abandoned = await sweep_jobs(session, queue)
    except Exception:
        log.exception("worker.sweep_failed")  # not fatal: the next sweep tries again
        return
    if requeued or abandoned:
        log.warning("worker.swept", requeued=requeued, abandoned=abandoned)


async def run_worker(settings: Settings, stop: asyncio.Event) -> None:
    engine = build_engine(settings.database_url)
    sessions = build_session_factory(engine)
    queue = build_job_queue(settings)
    storage = build_storage(settings)
    embeddings = build_embedding_provider(settings)
    log.info("worker.started", queue=settings.ingestion_queue_name, model=embeddings.model_id)
    next_sweep = 0.0
    try:
        while not stop.is_set():
            if time.monotonic() >= next_sweep:
                await sweep(sessions, queue)
                next_sweep = time.monotonic() + SWEEP_INTERVAL_SECONDS
            for message in await queue.receive():  # waits up to 10 s for a message
                await handle_message(
                    message,
                    queue=queue,
                    sessions=sessions,
                    storage=storage,
                    embeddings=embeddings,
                    max_receives=settings.ingestion_max_receives,
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
