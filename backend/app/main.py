import asyncio
import re
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException
import structlog

from app.api.routes import router
from app.core.config import PROJECT_ROOT, Settings, get_settings
from app.core.errors import (
    ApplicationError,
    application_exception_handler,
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from app.core.logging import configure_logging
from app.db.session import create_engine
from app.obsidian.state import watcher_state
from app.providers.models import (
    DraftProvider,
    EmbeddingProvider,
    FastEmbedEmbeddingProvider,
    OpenAICompatibleDraftProvider,
    OpenAICompatibleEmbeddingProvider,
    PassthroughDraftProvider,
    ProviderNotConfigured,
)
from app.services.jobs import JobRunner
from app.services.stage2 import Stage2Service


def normalize_request_id(value: str | None) -> str:
    if value and len(value) <= 80 and re.fullmatch(r"[A-Za-z0-9._-]+", value):
        return value
    return uuid4().hex


async def watcher_loop(stage2: Stage2Service, interval: float) -> None:
    watcher_state.running = True
    watcher_state.last_error = None
    try:
        while True:
            try:
                await stage2.rescan(minimum_file_age_seconds=max(interval * 2, 0.25))
                watcher_state.last_heartbeat_at = datetime.now(timezone.utc)
                watcher_state.last_error = None
            except Exception as error:
                watcher_state.last_error = type(error).__name__
            await asyncio.sleep(interval)
    finally:
        watcher_state.running = False


def create_app(
    settings: Settings | None = None,
    draft_provider: DraftProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    *,
    start_background: bool = True,
    serve_frontend: bool = True,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    engine = create_engine(resolved_settings)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    if draft_provider is None:
        try:
            draft_provider = OpenAICompatibleDraftProvider(resolved_settings)
        except ProviderNotConfigured:
            draft_provider = PassthroughDraftProvider()
    if embedding_provider is None:
        try:
            if resolved_settings.embedding_provider == "fastembed":
                embedding_provider = FastEmbedEmbeddingProvider(resolved_settings)
            else:
                embedding_provider = OpenAICompatibleEmbeddingProvider(resolved_settings)
        except ProviderNotConfigured:
            embedding_provider = None
    stage2 = Stage2Service(resolved_settings, session_factory, draft_provider, embedding_provider)
    runner = JobRunner(
        session_factory,
        handlers={"ingest_text": stage2.process_ingestion},
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        configure_logging(resolved_settings.log_level)
        structlog.get_logger("app").info(
            "application_started",
            app_env=resolved_settings.app_env,
            bind_host=resolved_settings.api_host,
            bind_port=resolved_settings.api_port,
        )
        tasks: list[asyncio.Task[None]] = []
        if start_background:
            try:
                await runner.recover_interrupted()
                tasks.append(asyncio.create_task(runner.run_forever()))
                if resolved_settings.vault_root is not None:
                    tasks.append(
                        asyncio.create_task(
                            watcher_loop(
                                stage2,
                                resolved_settings.obsidian_watch_interval_seconds,
                            )
                        )
                    )
            except Exception as error:
                structlog.get_logger("app").info(
                    "background_services_not_started",
                    error_type=type(error).__name__,
                )
        yield
        runner.stop()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await engine.dispose()
        structlog.get_logger("app").info("application_stopped")

    app = FastAPI(
        title=resolved_settings.app_name,
        version="0.2.0",
        description="知流台本机优先知识工作台 API",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.stage2_service = stage2
    app.state.job_runner = runner
    frontend_dist = PROJECT_ROOT / "frontend" / "dist"
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            origin.strip() for origin in resolved_settings.cors_origins.split(",") if origin.strip()
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = normalize_request_id(request.headers.get("X-Request-ID"))
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        structlog.get_logger("http").info(
            "request_completed",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
        )
        return response

    @app.get("/api/meta", include_in_schema=False)
    async def metadata() -> dict[str, str]:
        return {"name": "知流台 API", "status": "running", "docs": "/docs"}

    @app.get("/", include_in_schema=False)
    async def root():
        index = frontend_dist / "index.html"
        if serve_frontend and index.is_file():
            return FileResponse(index)
        return await metadata()

    app.include_router(router)
    app.add_exception_handler(ApplicationError, application_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
    if serve_frontend and frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
    return app


app = create_app()
