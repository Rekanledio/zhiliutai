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

from app.api.rag_routes import router as rag_router
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
from app.core.safety import redact_sensitive_text
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
from app.providers.capabilities import (
    FasterWhisperASRProvider,
    FfmpegKeyframeSampler,
    OpenAICompatibleVisionProvider,
)
from app.providers.rag import OpenAICompatibleRagChatProvider, RagChatProvider
from app.providers.video import ASRProvider, OCRProvider, SceneDetector, VideoSourceProvider, VisionProvider
from app.rag.citations import CitationBuilder
from app.rag.question_answer import QuestionAnswerService
from app.rag.reranking import RerankerProvider, SentenceTransformersReranker
from app.rag.retrieval import HybridRetriever
from app.services.backup import BackupRestoreService
from app.services.jobs import JobRunner
from app.services.knowledge import KnowledgeApplicationService
from app.services.maintenance import MaintenanceCoordinator
from app.services.stage2 import Stage2Service
from app.workflows.production import (
    IngestionWorkflowCoordinator,
    Stage2IngestionWorkflowServices,
)
from app.workflows.question_answer_production import (
    ProductionQuestionAnswerWorkflowServices,
    QuestionAnswerWorkflowCoordinator,
)
from app.workflows.runtime import WorkflowRuntime


def normalize_request_id(value: str | None) -> str:
    if value and len(value) <= 80 and re.fullmatch(r"[A-Za-z0-9._-]+", value):
        return value
    return uuid4().hex


async def watcher_loop(
    stage2: Stage2Service,
    interval: float,
    maintenance: MaintenanceCoordinator,
) -> None:
    watcher_state.running = True
    watcher_state.last_error = None
    try:
        while True:
            try:
                result = await maintenance.rescan(
                    minimum_file_age_seconds=max(interval * 2, 0.25),
                    skip_if_busy=True,
                )
                if result is not None:
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
    rag_chat_provider: RagChatProvider | None = None,
    reranker: RerankerProvider | None = None,
    video_provider: VideoSourceProvider | None = None,
    asr_provider: ASRProvider | None = None,
    audio_extractor=None,
    scene_detector: SceneDetector | None = None,
    vision_provider: VisionProvider | None = None,
    ocr_provider: OCRProvider | None = None,
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
    if rag_chat_provider is None:
        try:
            rag_chat_provider = OpenAICompatibleRagChatProvider(resolved_settings)
        except ProviderNotConfigured:
            rag_chat_provider = None
    if asr_provider is None and resolved_settings.asr_provider == "faster-whisper":
        try:
            asr_provider = FasterWhisperASRProvider(resolved_settings)
        except ProviderNotConfigured:
            asr_provider = None
    if vision_provider is None:
        try:
            vision_provider = OpenAICompatibleVisionProvider(resolved_settings)
        except ProviderNotConfigured:
            vision_provider = None
    if scene_detector is None and vision_provider is not None:
        scene_detector = FfmpegKeyframeSampler(
            resolved_settings.artifact_root,
            executable=resolved_settings.video_ffmpeg_executable,
            interval_seconds=resolved_settings.video_vision_keyframe_interval_seconds,
            configured_max_keyframes=resolved_settings.video_vision_max_keyframes,
            timeout_seconds=resolved_settings.video_fetch_timeout,
        )
    if (
        reranker is None
        and resolved_settings.reranker_provider == "sentence-transformers"
        and resolved_settings.reranker_model
    ):
        reranker = SentenceTransformersReranker(
            resolved_settings.reranker_model,
            device=resolved_settings.reranker_device,
            cache_path=resolved_settings.reranker_cache_path,
            local_files_only=resolved_settings.reranker_local_files_only,
        )
    stage2 = Stage2Service(
        resolved_settings,
        session_factory,
        draft_provider,
        embedding_provider,
        video_provider=video_provider,
        asr_provider=asr_provider,
        audio_extractor=audio_extractor,
        scene_detector=scene_detector,
        vision_provider=vision_provider,
        ocr_provider=ocr_provider,
    )
    rag_retriever = HybridRetriever(
        session_factory,
        stage2.vector_store,
        embedding_provider,
        resolved_settings,
        reranker=reranker,
    )
    citation_builder = CitationBuilder(
        session_factory,
        artifact_store=stage2.artifacts,
        vault=stage2.vault() if resolved_settings.vault_root is not None else None,
    )
    question_answer_service = QuestionAnswerService(
        session_factory,
        rag_retriever,
        citation_builder,
        rag_chat_provider,
        mutation_lock=stage2.mutation_lock,
    )
    knowledge_service = KnowledgeApplicationService(
        stage2,
        rag_retriever,
        citation_builder,
        session_factory,
    )
    backup_service = BackupRestoreService(
        resolved_settings,
        session_factory,
        embedding_provider,
    )
    maintenance_service = MaintenanceCoordinator(stage2, backup_service)
    ingestion_workflow_services = Stage2IngestionWorkflowServices(stage2, session_factory)
    question_answer_workflow_services = ProductionQuestionAnswerWorkflowServices(
        question_answer_service
    )
    workflow_runtime = WorkflowRuntime(
        ingestion_services=ingestion_workflow_services,
        question_answer_services=question_answer_workflow_services,
        settings=resolved_settings,
    )
    ingestion_workflow = IngestionWorkflowCoordinator(
        workflow_runtime,
        ingestion_workflow_services,
        session_factory,
    )
    question_answer_workflow = QuestionAnswerWorkflowCoordinator(
        workflow_runtime,
        question_answer_workflow_services,
    )
    runner = JobRunner(
        session_factory,
        handlers={
            "ingest_text": ingestion_workflow.run_job,
            "ingest_source": ingestion_workflow.run_job,
            "ingest_video": ingestion_workflow.run_job,
        },
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
        runtime_open = False
        try:
            await workflow_runtime.__aenter__()
            runtime_open = True
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
                                    maintenance_service,
                                )
                            )
                        )
                except Exception as error:
                    structlog.get_logger("app").info(
                        "background_services_not_started",
                        error_type=type(error).__name__,
                    )
            yield
        finally:
            runner.stop()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            if runtime_open:
                await workflow_runtime.aclose()
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
    app.state.rag_retriever = rag_retriever
    app.state.citation_builder = citation_builder
    app.state.rag_chat_provider = rag_chat_provider
    app.state.question_answer_service = question_answer_service
    app.state.knowledge_service = knowledge_service
    app.state.embedding_provider = embedding_provider
    app.state.backup_service = backup_service
    app.state.maintenance_service = maintenance_service
    app.state.workflow_runtime = workflow_runtime
    app.state.ingestion_workflow = ingestion_workflow
    app.state.question_answer_workflow = question_answer_workflow
    app.state.question_answer_workflow_services = question_answer_workflow_services
    app.state.job_runner = runner
    app.state.video_service = stage2.video_service
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
            path=redact_sensitive_text(request.url.path),
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
    app.include_router(rag_router)
    app.add_exception_handler(ApplicationError, application_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
    if serve_frontend and frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
    return app


app = create_app()
