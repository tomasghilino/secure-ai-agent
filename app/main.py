"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.health import router as health_router
from app.api.search import router as search_router
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.database.session import build_engine, build_session_factory
from app.rag.embeddings import build_embedding_provider


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    log = get_logger(__name__)
    # Created once per process and shared by every request (see app/api/dependencies.py).
    # Creating the engine does not connect yet: connections open on first use.
    engine = build_engine(settings.database_url)
    app.state.session_factory = build_session_factory(engine)
    app.state.embeddings = build_embedding_provider(settings)
    log.info(
        "app.startup",
        environment=settings.app_env,
        llm_provider=settings.llm_provider,
        embedding_provider=settings.embedding_provider,
    )
    yield
    await engine.dispose()  # close the pooled connections
    log.info("app.shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    app = FastAPI(
        title="Northstar AI Operations Copilot",
        version=__version__,
        lifespan=lifespan,
    )
    app.include_router(health_router)
    app.include_router(search_router)
    return app


app = create_app()
