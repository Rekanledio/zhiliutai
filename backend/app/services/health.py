import ipaddress
import os
import shutil
import sqlite3
import time
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
_MAX_HEALTH_CHECK_TIMEOUT_SECONDS = 1.0


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


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _health_check_timeout(settings: Settings) -> float:
    return min(
        max(float(settings.health_check_timeout), 0.01),
        _MAX_HEALTH_CHECK_TIMEOUT_SECONDS,
    )


async def _probe_http_model(
    settings: Settings,
    base_url: str,
    capability: str,
    api_key: str | None,
) -> tuple[str, str, float | None]:
    try:
        parsed = urlsplit(base_url)
        # Accessing port validates malformed bracketed/port authority values.
        _ = parsed.port
    except ValueError:
        return "degraded", f"{capability} 地址格式无效", None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in base_url)
    ):
        return "degraded", f"{capability} 地址格式无效", None
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    local_without_key = not api_key and _is_loopback_host(parsed.hostname)
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=_health_check_timeout(settings),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.get(f"{base_url.rstrip('/')}/models", headers=headers)
    except httpx.TimeoutException:
        logger.info("model_probe_timeout", capability=capability)
        return "degraded", f"{capability} 探针超时", None
    except httpx.ConnectError:
        logger.info("model_probe_connection_failed", capability=capability)
        return "degraded", f"{capability} 服务不可达", None
    except httpx.HTTPError as error:
        logger.info(
            "model_probe_http_failed",
            capability=capability,
            error_type=_safe_error_type(error),
        )
        return "degraded", f"{capability} 探针请求失败", None
    except Exception as error:
        logger.info("model_probe_failed", error_type=_safe_error_type(error))
        return "degraded", f"{capability} 探针失败", None
    latency = round((time.perf_counter() - started) * 1000, 1)
    if 200 <= response.status_code < 300:
        endpoint_kind = "本地端点" if local_without_key else "端点"
        return (
            "healthy",
            f"{capability} {endpoint_kind}已验证（HTTP {response.status_code}）",
            latency,
        )
    if response.status_code in {401, 403}:
        return (
            "degraded",
            f"{capability} 端点认证未通过（HTTP {response.status_code}）",
            latency,
        )
    if 300 <= response.status_code < 400:
        return (
            "degraded",
            f"{capability} 重定向被拒绝（HTTP {response.status_code}）",
            latency,
        )
    if 400 <= response.status_code < 500:
        return (
            "degraded",
            f"{capability} 端点返回客户端错误（HTTP {response.status_code}）",
            latency,
        )
    if response.status_code >= 500:
        return (
            "degraded",
            f"{capability} 端点返回服务端错误（HTTP {response.status_code}）",
            latency,
        )
    return (
        "degraded",
        f"{capability} 端点返回异常状态（HTTP {response.status_code}）",
        latency,
    )


async def probe_model_providers(settings: Settings) -> HealthComponent:
    local_configured = False
    local_failures: list[str] = []
    local_summaries: list[str] = []
    if settings.embedding_provider == "fastembed" and settings.embedding_model:
        cache = probe_writable_directory(
            "embedding_cache",
            "Embedding Cache",
            settings.embedding_cache_path,
            create=True,
        )
        local_configured = True
        if cache.state != "healthy":
            local_failures.append("Embedding")
            local_summaries.append("Embedding=FastEmbed 缓存不可用")
        else:
            local_summaries.append("Embedding=FastEmbed（首次调用时加载）")
    elif settings.embedding_provider == "fastembed":
        local_summaries.append("Embedding=未配置")

    if settings.asr_provider == "faster-whisper" and settings.asr_model:
        cache = probe_writable_directory(
            "asr_cache",
            "ASR Cache",
            settings.asr_cache_path,
            create=True,
        )
        local_configured = True
        if cache.state != "healthy":
            local_failures.append("ASR")
            local_summaries.append("ASR=faster-whisper 缓存不可用")
        else:
            local_summaries.append("ASR=faster-whisper（首次调用时加载）")
    elif settings.asr_provider == "faster-whisper":
        local_summaries.append("ASR=未配置")

    if settings.reranker_provider == "sentence-transformers" and settings.reranker_model:
        cache = probe_writable_directory(
            "reranker_cache",
            "Reranker Cache",
            settings.reranker_cache_path,
            create=True,
        )
        local_configured = True
        if cache.state != "healthy":
            local_failures.append("Reranker")
            local_summaries.append("Reranker=SentenceTransformers 缓存不可用")
        else:
            local_summaries.append("Reranker=SentenceTransformers（首次调用时加载）")
    elif settings.reranker_provider == "sentence-transformers":
        local_summaries.append("Reranker=未配置")

    local_detail = "本地能力：" + ("、".join(local_summaries) or "未配置")

    capabilities = [
        ("Chat", settings.chat_base_url, settings.chat_model, settings.chat_api_key),
    ]
    if settings.asr_provider == "openai-compatible":
        capabilities.append(
            ("ASR", settings.asr_base_url, settings.asr_model, settings.asr_api_key)
        )
    capabilities.append(
        ("Vision", settings.vision_base_url, settings.vision_model, settings.vision_api_key)
    )
    if settings.reranker_provider == "openai-compatible":
        capabilities.append(
            (
                "Reranker",
                settings.reranker_base_url,
                settings.reranker_model,
                settings.reranker_api_key,
            )
        )
    if settings.embedding_provider == "openai-compatible":
        capabilities.insert(
            1,
            (
                "Embedding",
                settings.embedding_base_url,
                settings.embedding_model,
                settings.embedding_api_key,
            ),
        )
    remote_summaries: list[str] = []
    remote_failures: list[str] = []
    remote_configured = False
    latencies: list[float] = []
    for name, url, model, key in capabilities:
        if url and model:
            remote_configured = True
            state, detail, latency = await _probe_http_model(settings, url, name, key)
            remote_summaries.append(detail)
            if state != "healthy":
                remote_failures.append(name)
            if latency is not None:
                latencies.append(latency)
        elif url or model:
            remote_failures.append(name)
            remote_summaries.append(f"{name}=配置不完整")
        else:
            remote_summaries.append(f"{name}=未配置")

    if not remote_configured and not local_configured and not remote_failures:
        return _component(
            "model_providers",
            "Model Providers",
            "not_configured",
            f"{local_detail}；远程能力：{'、'.join(remote_summaries)}",
        )
    failures = [*local_failures, *remote_failures]
    detail = f"{local_detail}；远程能力：{'、'.join(remote_summaries)}"
    if failures:
        return _component(
            "model_providers",
            "Model Providers",
            "degraded",
            detail,
            max(latencies, default=None),
        )
    aggregate_state = "configured" if local_configured else "healthy"
    return _component(
        "model_providers",
        "Model Providers",
        aggregate_state,
        detail,
        max(latencies, default=None),
    )


def probe_ffmpeg(
    which: Callable[[str], str | None] | None = None,
    *,
    executable: str = "ffmpeg",
) -> HealthComponent:
    resolver = shutil.which if which is None else which
    try:
        resolved = resolver(executable)
    except (OSError, TypeError, ValueError):
        resolved = None
    if resolved is None:
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
        probe_ffmpeg(executable=settings.video_ffmpeg_executable),
    ]
    required = {"api", "sqlite", "qdrant", "artifact_storage"}
    overall = "healthy"
    if any(item.key in required and item.state != "healthy" for item in components):
        overall = "degraded"
    if any(
        item.key not in required
        and item.state in {"degraded", "unavailable", "not_configured"}
        for item in components
    ):
        overall = "degraded"
    return HealthResponse(
        status=overall,
        checked_at=datetime.now().astimezone(),
        components=components,
    )
