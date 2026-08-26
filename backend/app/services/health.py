import shutil
import sqlite3
import time
import os
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import structlog
from qdrant_client import QdrantClient

from app.core.config import Settings
from app.obsidian.state import watcher_state
from app.schemas.health import HealthComponent, HealthResponse

logger = structlog.get_logger("health")


def _component(
    key: str,
    label: str,
    state: str,
    detail: str,
    latency_ms: float | None = None,
) -> HealthComponent:
    return HealthComponent(
        key=key,
        label=label,
        state=state,
        detail=detail,
        latency_ms=latency_ms,
    )


def _safe_error_type(error: Exception) -> str:
    return type(error).__name__


def probe_sqlite(settings: Settings) -> HealthComponent:
    key, label = "sqlite", "SQLite"
    path = settings.database_path
    started = time.perf_counter()
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            path,
            timeout=settings.sqlite_busy_timeout_ms / 1000,
        )
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}")
        journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        connection.execute("SELECT 1").fetchone()
    except Exception as error:
        logger.info("sqlite_probe_failed", error_type=_safe_error_type(error))
        return _component(key, label, "unavailable", "数据库无法打开或执行查询")
    finally:
        if connection is not None:
            connection.close()
    mode = str(journal_mode[0]).lower() if journal_mode else "unknown"
    return _component(
        key,
        label,
        "healthy",
        f"本地数据库可用；journal_mode={mode}",
        round((time.perf_counter() - started) * 1000, 1),
    )


def probe_qdrant(settings: Settings) -> HealthComponent:
    key, label = "qdrant", "Qdrant Local"
    started = time.perf_counter()
    client: QdrantClient | None = None
    try:
        settings.qdrant_path.mkdir(parents=True, exist_ok=True)
        client = QdrantClient(path=str(settings.qdrant_path))
        client.get_collections()
    except Exception as error:
        logger.info("qdrant_probe_failed", error_type=_safe_error_type(error))
        return _component(key, label, "unavailable", "本地向量目录无法初始化")
    finally:
        if client is not None:
            client.close()
    return _component(
        key,
        label,
        "healthy",
        "本地持久化客户端可用",
        round((time.perf_counter() - started) * 1000, 1),
    )


def probe_writable_directory(
    key: str, label: str, root: Path, *, create: bool = False
) -> HealthComponent:
    marker: Path | None = None
    try:
        if create:
            root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            return _component(key, label, "unavailable", "目录不存在或不是目录")
        marker = root / f".health-{uuid4().hex}.tmp"
        marker.write_bytes(b"ok")
        if marker.read_bytes() != b"ok":
            raise OSError("health marker mismatch")
    except Exception as error:
        logger.info(f"{key}_probe_failed", error_type=_safe_error_type(error))
        return _component(key, label, "unavailable", "目录不可读写")
    finally:
        if marker is not None:
            marker.unlink(missing_ok=True)
    return _component(key, label, "healthy", "目录可读写")


def probe_obsidian(settings: Settings) -> HealthComponent:
    root = settings.vault_root
    if root is None:
        return _component("obsidian", "Obsidian Vault", "not_configured", "尚未配置 Vault 路径")
    if not root.is_dir():
        return _component("obsidian", "Obsidian Vault", "unavailable", "Vault 路径不存在或不是目录")
    if not os.access(root, os.R_OK | os.W_OK):
        return _component("obsidian", "Obsidian Vault", "unavailable", "Vault 路径不可读写")
    return _component(
        "obsidian",
        "Obsidian Vault",
        "healthy",
        "Vault 路径可访问；受管理目录按发布需要创建",
    )


def probe_obsidian_watcher(settings: Settings) -> HealthComponent:
    if settings.vault_root is None:
        return _component(
            "obsidian_watcher",
            "Obsidian Watcher",
            "not_configured",
            "Vault 未配置，监听器未启动",
        )
    if not watcher_state.running:
        return _component(
            "obsidian_watcher",
            "Obsidian Watcher",
            "degraded",
            "Vault 已配置，但监听器未运行",
        )
    return _component(
        "obsidian_watcher",
        "Obsidian Watcher",
        "healthy",
        "监听器运行中",
    )


async def _probe_http_model(
    settings: Settings,
    base_url: str,
    model: str,
    api_key: str | None,
) -> tuple[str, str, float | None]:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "degraded", f"{model} 地址格式无效", None
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=settings.health_check_timeout, follow_redirects=False
        ) as client:
            response = await client.get(f"{base_url.rstrip('/')}/models", headers=headers)
    except Exception as error:
        logger.info("model_probe_failed", error_type=_safe_error_type(error))
        return "degraded", f"{model} 服务不可达", None
    latency = round((time.perf_counter() - started) * 1000, 1)
    if response.status_code >= 500:
        return "degraded", f"{model} 服务返回错误", latency
    return "configured", f"{model} 服务可达，能力将在调用时验证", latency


async def probe_model_providers(settings: Settings) -> HealthComponent:
    capabilities = [
        ("Chat", settings.chat_base_url, settings.chat_model, settings.chat_api_key),
        (
            "Embedding",
            settings.embedding_base_url,
            settings.embedding_model,
            settings.embedding_api_key,
        ),
        ("ASR", settings.asr_base_url, settings.asr_model, None),
        ("Vision", settings.vision_base_url, settings.vision_model, None),
        ("Reranker", settings.reranker_base_url, settings.reranker_model, None),
    ]
    configured = [
        (name, url, model, key)
        for name, url, model, key in capabilities
        if url and model
    ]
    if not configured:
        return _component(
            "model_providers",
            "Model Providers",
            "not_configured",
            "Chat、Embedding、ASR、Vision、Reranker 均未配置",
        )
    failures: list[str] = []
    latencies: list[float] = []
    for name, url, model, key in configured:
        assert url is not None and model is not None
        state, detail, latency = await _probe_http_model(settings, url, model, key)
        if state == "degraded":
            failures.append(name)
        if latency is not None:
            latencies.append(latency)
    names = "、".join(name for name, *_ in configured)
    if failures:
        return _component(
            "model_providers",
            "Model Providers",
            "degraded",
            f"不可达能力：{'、'.join(failures)}；已配置：{names}",
            max(latencies, default=None),
        )
    return _component(
        "model_providers",
        "Model Providers",
        "configured",
        f"已配置且端点可达：{names}",
        max(latencies, default=None),
    )


def probe_ffmpeg(which: Callable[[str], str | None] = shutil.which) -> HealthComponent:
    executable = which("ffmpeg")
    if executable is None:
        return _component(
            "ffmpeg",
            "FFmpeg",
            "not_configured",
            "当前未安装；仅影响后续视频能力",
        )
    return _component("ffmpeg", "FFmpeg", "healthy", "命令可用")


async def build_health_report(settings: Settings) -> HealthResponse:
    components = [
        _component("api", "FastAPI", "healthy", "服务进程正常"),
        probe_sqlite(settings),
        probe_qdrant(settings),
        probe_writable_directory(
            "artifact_storage", "Artifact Storage", settings.artifact_root, create=True
        ),
        probe_obsidian(settings),
        probe_obsidian_watcher(settings),
        await probe_model_providers(settings),
        probe_ffmpeg(),
    ]
    required = {"api", "sqlite", "qdrant", "artifact_storage"}
    overall = "healthy"
    if any(item.key in required and item.state != "healthy" for item in components):
        overall = "degraded"
    if any(
        item.key not in required and item.state in {"degraded", "unavailable"}
        for item in components
    ):
        overall = "degraded"
    return HealthResponse(
        status=overall,
        checked_at=datetime.now().astimezone(),
        components=components,
    )
