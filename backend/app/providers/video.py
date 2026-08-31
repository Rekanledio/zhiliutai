"""Video acquisition/tool adapters and provider-neutral ASR/Vision contracts.

yt-dlp and FFmpeg are local-tool adapters with closed subprocess boundaries.
ASR, Vision, and OCR remain protocols plus deterministic fakes because no
compatible production HTTP protocol has been selected for those providers.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import socket
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence, runtime_checkable
from urllib.parse import urlsplit

from app.ingestion.fetcher import ResolveHost, UnsafeUrlError, validate_public_url
from app.ingestion.safe_proxy import ConnectTarget, LoopbackSafeProxy, open_numeric_target
from app.video.types import (
    Keyframe,
    SubtitleTrack,
    TranscriptSegment,
    VideoSourceMetadata,
    VisualEvent,
)


class VideoProviderError(RuntimeError):
    """Safe, structured provider failure without upstream response content."""


class VideoCapabilityError(VideoProviderError):
    """The selected local tool or capability is not available."""


class VideoLimitError(VideoProviderError):
    """A provider result exceeded a configured bound."""


class VideoSecurityError(VideoProviderError):
    """A provider returned an unsafe URL or filesystem result."""


@dataclass(frozen=True)
class VideoDownloadOptions:
    max_bytes: int
    max_duration_ms: int
    timeout_seconds: float
    max_redirects: int
    subtitle_languages: tuple[str, ...] = ()
    max_subtitle_bytes: int = 10_000_000


@dataclass(frozen=True)
class VideoDownloadResult:
    metadata: VideoSourceMetadata
    media_path: Path | None
    subtitle_tracks: tuple[SubtitleTrack, ...] = ()
    redirect_chain: tuple[str, ...] = ()
    network_policy_enforced: bool = False


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


class CommandRunner(Protocol):
    async def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


@dataclass(frozen=True)
class YtDlpExecutionResult:
    """Result returned by a network boundary that owns every HTTP hop.

    The redirect chain, when available, is evidence produced by the boundary,
    not by yt-dlp. A production implementation must resolve and validate every
    destination immediately before connect and enforce bounded execution. The
    adapter refuses to run without this boundary.
    """

    command: CommandResult
    redirect_chain: tuple[str, ...] = ()
    network_policy_enforced: bool = False


@runtime_checkable
class YtDlpNetworkExecutor(Protocol):
    """Closed network execution boundary for the yt-dlp child process.

    Implementations must place the child behind a controlled local proxy or an
    equivalent network namespace. They, rather than yt-dlp, are responsible
    for per-destination scheme/DNS/IP validation, DNS-rebinding resistance,
    request and timeout bounds. No direct subprocess execution is a supported
    production fallback.
    """

    proxy_url: str

    async def execute(
        self,
        runner: CommandRunner,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
        env: Mapping[str, str],
        requested_url: str,
        options: VideoDownloadOptions,
    ) -> YtDlpExecutionResult: ...


@runtime_checkable
class VideoDownloader(Protocol):
    async def download(
        self,
        url: str,
        *,
        destination: Path,
        options: VideoDownloadOptions,
    ) -> VideoDownloadResult: ...


@runtime_checkable
class VideoSourceProvider(Protocol):
    async def acquire(
        self,
        url: str,
        *,
        destination: Path,
        options: VideoDownloadOptions,
    ) -> VideoDownloadResult: ...


@dataclass(frozen=True)
class AudioExtractionOptions:
    max_bytes: int
    timeout_seconds: float


@runtime_checkable
class AudioExtractor(Protocol):
    async def extract(
        self,
        media_path: Path,
        *,
        destination: Path,
        options: AudioExtractionOptions,
    ) -> Path: ...


@dataclass(frozen=True)
class FrameSample:
    keyframe: Keyframe
    image: bytes


@runtime_checkable
class SceneDetector(Protocol):
    async def detect(
        self,
        media_path: Path,
        *,
        metadata: VideoSourceMetadata,
        destination: Path,
        max_keyframes: int,
    ) -> Sequence[FrameSample]: ...


class SubprocessCommandRunner:
    """Run a fixed executable without a shell and without logging its output."""

    async def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(cwd),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(env) if env is not None else None,
            )
        except FileNotFoundError as error:
            raise VideoCapabilityError("视频处理工具不可用") from error
        except OSError as error:
            raise VideoProviderError("视频处理工具无法启动") from error
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError as error:
            process.kill()
            await process.communicate()
            raise VideoProviderError("视频处理工具执行超时") from error
        if len(stdout) > 2_000_000 or len(stderr) > 2_000_000:
            raise VideoProviderError("视频处理工具输出超过限制")
        return CommandResult(process.returncode or 0, stdout, stderr)


def _within_root(path: Path, root: Path) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise VideoSecurityError("视频工具目录越界")
    return resolved


def _isolated_ytdlp_environment(workdir: Path) -> dict[str, str]:
    """Return an environment that cannot import ambient proxy/config state."""

    state_root = workdir / ".ytdlp-state"
    for child in (state_root, state_root / "config", state_root / "cache"):
        child.mkdir(parents=True, exist_ok=True)

    environment: dict[str, str] = {}
    # PATH is only retained for executable lookup.  SystemRoot is required by
    # Windows process/runtime libraries.  No ambient proxy, credential,
    # Python, yt-dlp, or configuration variables are inherited.
    for name in ("PATH", "PATHEXT", "SystemRoot", "WINDIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    environment.update(
        {
            "HOME": str(state_root),
            "USERPROFILE": str(state_root),
            "APPDATA": str(state_root / "config"),
            "LOCALAPPDATA": str(state_root / "config"),
            "XDG_CONFIG_HOME": str(state_root / "config"),
            "XDG_CACHE_HOME": str(state_root / "cache"),
            "TMP": str(workdir),
            "TEMP": str(workdir),
        }
    )
    return environment


def _controlled_proxy_url(value: object) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise VideoSecurityError("视频安全网络代理配置无效")
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise VideoSecurityError("视频安全网络代理配置无效")
    try:
        port = parsed.port
    except ValueError as error:
        raise VideoSecurityError("视频安全网络代理配置无效") from error
    if port is None or not 1 <= port <= 65535:
        raise VideoSecurityError("视频安全网络代理配置无效")
    return value


class YtDlpDownloader:
    """Controlled yt-dlp adapter with a closed argument and network allowlist.

    yt-dlp cannot prove that its internal redirect/DNS resolution was safe for
    this application. Therefore this adapter has no direct-run fallback: its
    default loopback executor, or an injected equivalent, owns every network
    connection.
    """

    provider = "yt-dlp"
    tool_version = "adapter-v2-network-gated"

    def __init__(
        self,
        root: Path,
        *,
        executable: str = "yt-dlp",
        runner: CommandRunner | None = None,
        url_validator: Callable[[str], None] = validate_public_url,
        network_executor: YtDlpNetworkExecutor | None = None,
    ) -> None:
        self.root = root.resolve()
        self.executable = executable
        self.runner = runner or SubprocessCommandRunner()
        self.url_validator = url_validator
        self.network_executor = network_executor

    async def download(
        self,
        url: str,
        *,
        destination: Path,
        options: VideoDownloadOptions,
    ) -> VideoDownloadResult:
        try:
            self.url_validator(url)
        except (UnsafeUrlError, ValueError) as error:
            raise VideoSecurityError("视频来源 URL 不安全") from error
        workdir = _within_root(destination, self.root)
        workdir.mkdir(parents=True, exist_ok=True)
        network_executor = self.network_executor or LoopbackYtDlpNetworkExecutor()
        proxy_url = _controlled_proxy_url(network_executor.proxy_url)
        environment = _isolated_ytdlp_environment(workdir)
        languages = ",".join(options.subtitle_languages) if options.subtitle_languages else "all"
        # This list is intentionally assembled here, not from request data.
        # Config/plugins/cookies/credentials and arbitrary downloader flags
        # cannot enter the subprocess boundary.  The proxy is supplied only
        # by the injected, controlled network executor.
        args = [
            self.executable,
            "--ignore-config",
            "--no-plugin-dirs",
            "--no-playlist",
            "--no-progress",
            "--no-warnings",
            "--no-cache-dir",
            "--no-call-home",
            "--proxy",
            proxy_url,
            # Never delegate network downloads to ffmpeg/curl/aria2c/etc.;
            # only yt-dlp's in-process downloader is covered by this proxy
            # and scrubbed environment boundary.
            "--downloader",
            "native",
            "--restrict-filenames",
            "--max-downloads",
            "1",
            "--max-filesize",
            str(options.max_bytes),
            "--socket-timeout",
            str(max(1, int(options.timeout_seconds))),
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            languages,
            "--sub-format",
            "vtt/srt/best",
            "--print-json",
            "--paths",
            f"home:{workdir}",
            "--output",
            "zhiliutai-%(id)s.%(ext)s",
            "--",
            url,
        ]
        execution = await network_executor.execute(
            self.runner,
            args,
            cwd=workdir,
            timeout=options.timeout_seconds,
            env=environment,
            requested_url=url,
            options=options,
        )
        if not execution.network_policy_enforced:
            raise VideoSecurityError("视频安全网络执行器未提供连接策略证明")
        result = execution.command
        # yt-dlp raises MaxDownloadsReached after completing the first item
        # when --max-downloads 1 is active. The CLI maps that successful,
        # bounded stop to 101, so validate its outputs below instead of
        # treating it as an upstream acquisition failure.
        if result.returncode not in {0, 101}:
            raise VideoProviderError("视频来源获取失败")
        payload = self._metadata(result.stdout)
        duration_ms = self._duration_ms(payload.get("duration"), options.max_duration_ms)
        final_url = payload.get("webpage_url")
        if not isinstance(final_url, str) or not final_url:
            final_url = url
        redirect_chain = tuple(execution.redirect_chain)
        if redirect_chain:
            if (
                redirect_chain[0] != url
                or len(redirect_chain) > options.max_redirects + 1
                or redirect_chain[-1] != final_url
            ):
                raise VideoSecurityError("视频来源重定向链无效")
            for hop in redirect_chain:
                try:
                    self.url_validator(hop)
                except (UnsafeUrlError, ValueError) as error:
                    raise VideoSecurityError("视频来源重定向链不安全") from error
        elif final_url != url and not execution.network_policy_enforced:
            # yt-dlp's metadata is not a redirect audit trail.  Do not invent
            # a chain from the requested and final URLs.
            raise VideoSecurityError("视频来源缺少可验证的重定向链")
        metadata = VideoSourceMetadata(
            source_url=final_url,
            requested_url=url,
            final_url=final_url,
            title=self._text(payload.get("title"), 500),
            duration_ms=duration_ms,
            media_type=self._text(payload.get("ext"), 100),
            video_id=self._text(payload.get("id"), 300),
            uploader=self._text(payload.get("uploader"), 300),
            width_px=self._positive_int(payload.get("width")),
            height_px=self._positive_int(payload.get("height")),
            frame_rate=self._finite_float(payload.get("fps")),
            is_live=bool(payload.get("is_live", False)),
            video_kind="other",
            provider=self.provider,
            tool_version=self.tool_version,
        )
        media_path = self._media_file(workdir, options.max_bytes)
        subtitles: list[SubtitleTrack] = []
        for path in sorted(workdir.iterdir(), key=lambda candidate: candidate.name):
            if not path.is_file() or path.suffix.casefold() not in {".vtt", ".srt", ".json3"}:
                continue
            if path.stat().st_size > options.max_subtitle_bytes:
                raise VideoLimitError("字幕文件超过大小限制")
            fmt = path.suffix.casefold().removeprefix(".")
            stem_parts = path.stem.split(".")
            language = stem_parts[-1] if len(stem_parts) > 1 else None
            subtitles.append(
                SubtitleTrack(
                    content=path.read_bytes(),
                    format=fmt if fmt in {"vtt", "srt", "json3"} else "unknown",
                    language=language,
                    is_automatic="auto" in path.name.casefold(),
                    provider=self.provider,
                    tool_version=self.tool_version,
                )
            )
        if result.returncode == 101 and media_path is None and not subtitles:
            raise VideoProviderError("视频来源获取失败")
        return VideoDownloadResult(
            metadata,
            media_path,
            tuple(subtitles),
            redirect_chain,
            network_policy_enforced=True,
        )

    @staticmethod
    def _metadata(stdout: bytes) -> dict[str, object]:
        for line in reversed(stdout.decode("utf-8", errors="replace").splitlines()):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
        raise VideoProviderError("视频来源元数据无效")

    @staticmethod
    def _duration_ms(value: object, maximum: int) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise VideoProviderError("视频时长元数据无效")
        if not math.isfinite(float(value)) or value < 0:
            raise VideoProviderError("视频时长元数据无效")
        duration_ms = int(round(float(value) * 1000))
        if duration_ms > maximum:
            raise VideoLimitError("视频时长超过限制")
        return duration_ms

    @staticmethod
    def _text(value: object, maximum: int) -> str | None:
        if not isinstance(value, str):
            return None
        value = " ".join(value.split())
        return value[:maximum] or None

    @staticmethod
    def _positive_int(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None

    @staticmethod
    def _finite_float(value: object) -> float | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
            return float(value) if value > 0 else None
        return None

    @staticmethod
    def _media_file(workdir: Path, max_bytes: int) -> Path | None:
        ignored = {".vtt", ".srt", ".json3", ".ass", ".lrc", ".part", ".ytdl", ".json"}
        candidates = [
            path
            for path in workdir.iterdir()
            if path.is_file() and path.suffix.casefold() not in ignored
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda path: path.stat().st_size, reverse=True)
        selected = candidates[0]
        if selected.stat().st_size > max_bytes:
            raise VideoLimitError("视频文件超过大小限制")
        return selected


class LoopbackYtDlpNetworkExecutor:
    """Production local proxy boundary for one yt-dlp invocation.

    The proxy validates and pins every numeric outbound destination before the
    socket is opened. It does not inspect HTTPS payloads and therefore does not
    pretend to provide a plaintext redirect audit trail; its security proof is
    that every actual connection passed through the policy boundary.
    """

    def __init__(
        self,
        *,
        resolve_host: ResolveHost = socket.getaddrinfo,
        connect_target: ConnectTarget = open_numeric_target,
        timeout: float = 30.0,
    ) -> None:
        self.proxy = LoopbackSafeProxy(
            resolve_host=resolve_host,
            connect_target=connect_target,
            timeout=timeout,
        )
        self.proxy_url = self.proxy.proxy_url

    @property
    def validated_targets(self) -> tuple[str, ...]:
        return self.proxy.validated_targets

    async def execute(
        self,
        runner: CommandRunner,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
        env: Mapping[str, str],
        requested_url: str,
        options: VideoDownloadOptions,
    ) -> YtDlpExecutionResult:
        del requested_url, options
        async with self.proxy:
            result = await runner.run(args, cwd=cwd, timeout=timeout, env=env)
        return YtDlpExecutionResult(
            result,
            (),
            network_policy_enforced=True,
        )


class DeterministicYtDlpNetworkExecutor:
    """Offline executor fake for adapter tests; it never opens a socket."""

    proxy_url = "http://127.0.0.1:18080"

    def __init__(self, redirect_chain: Sequence[str] = ()) -> None:
        self.redirect_chain = tuple(redirect_chain)
        self.calls: list[dict[str, object]] = []

    async def execute(
        self,
        runner: CommandRunner,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
        env: Mapping[str, str],
        requested_url: str,
        options: VideoDownloadOptions,
    ) -> YtDlpExecutionResult:
        self.calls.append(
            {
                "args": tuple(args),
                "cwd": cwd,
                "timeout": timeout,
                "env": dict(env),
                "requested_url": requested_url,
                "max_redirects": options.max_redirects,
            }
        )
        result = await runner.run(args, cwd=cwd, timeout=timeout, env=env)
        return YtDlpExecutionResult(
            result,
            self.redirect_chain or (requested_url,),
            network_policy_enforced=True,
        )


class YtDlpVideoSourceProvider:
    name = "yt-dlp"
    tool_version = "adapter-v1"

    def __init__(self, downloader: VideoDownloader) -> None:
        self.downloader = downloader

    async def acquire(
        self,
        url: str,
        *,
        destination: Path,
        options: VideoDownloadOptions,
    ) -> VideoDownloadResult:
        return await self.downloader.download(url, destination=destination, options=options)


class FakeVideoSourceProvider:
    """Deterministic, network-free source provider for tests and fixtures."""

    name = "fake-video"
    tool_version = "fake-video-v1"

    def __init__(
        self,
        *,
        metadata: VideoSourceMetadata | None = None,
        subtitles: Sequence[SubtitleTrack] = (),
        media: bytes | None = None,
    ) -> None:
        self.metadata = metadata
        self.subtitles = tuple(subtitles)
        self.media = media
        self.calls = 0

    async def acquire(
        self,
        url: str,
        *,
        destination: Path,
        options: VideoDownloadOptions,
    ) -> VideoDownloadResult:
        self.calls += 1
        if self.media is not None and len(self.media) > options.max_bytes:
            raise VideoLimitError("视频文件超过大小限制")
        destination.mkdir(parents=True, exist_ok=True)
        media_path = None
        if self.media is not None:
            media_path = destination / "provider-media.bin"
            media_path.write_bytes(self.media)
        metadata = self.metadata or VideoSourceMetadata(
            source_url=url,
            requested_url=url,
            final_url=url,
            duration_ms=1_000,
            provider=self.name,
            tool_version=self.tool_version,
        )
        if metadata.requested_url != url or metadata.final_url is None:
            metadata = metadata.model_copy(update={"requested_url": url, "final_url": url})
        return VideoDownloadResult(metadata, media_path, self.subtitles, (url, metadata.final_url or url))


class FfmpegAudioExtractor:
    """Controlled FFmpeg audio extraction boundary; no shell or user flags."""

    def __init__(
        self,
        root: Path,
        *,
        executable: str = "ffmpeg",
        runner: CommandRunner | None = None,
    ) -> None:
        self.root = root.resolve()
        self.executable = executable
        self.runner = runner or SubprocessCommandRunner()

    async def extract(
        self,
        media_path: Path,
        *,
        destination: Path,
        options: AudioExtractionOptions,
    ) -> Path:
        source = _within_root(media_path, self.root)
        workdir = _within_root(destination, self.root)
        if not source.is_file():
            raise VideoProviderError("视频媒体不可用")
        workdir.mkdir(parents=True, exist_ok=True)
        output = workdir / "audio.wav"
        args = [
            self.executable,
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            str(output),
        ]
        result = await self.runner.run(args, cwd=workdir, timeout=options.timeout_seconds)
        if result.returncode != 0 or not output.is_file():
            raise VideoProviderError("视频音轨提取失败")
        if output.stat().st_size > options.max_bytes:
            raise VideoLimitError("音轨文件超过大小限制")
        return output


class FakeAudioExtractor:
    """Deterministic audio extractor used by offline ASR fallback tests."""

    def __init__(self, audio: bytes = b"fake-audio") -> None:
        self.audio = audio
        self.calls = 0

    async def extract(
        self,
        media_path: Path,
        *,
        destination: Path,
        options: AudioExtractionOptions,
    ) -> Path:
        self.calls += 1
        if len(self.audio) > options.max_bytes:
            raise VideoLimitError("音轨文件超过大小限制")
        destination.mkdir(parents=True, exist_ok=True)
        output = destination / "audio.wav"
        output.write_bytes(self.audio)
        return output


class FakeSceneDetector:
    """Deterministic scene/keyframe detector with predeclared frame bytes."""

    def __init__(self, frames: Sequence[FrameSample] = ()) -> None:
        self.frames = tuple(frames)
        self.calls = 0

    async def detect(
        self,
        media_path: Path,
        *,
        metadata: VideoSourceMetadata,
        destination: Path,
        max_keyframes: int,
    ) -> list[FrameSample]:
        self.calls += 1
        if len(self.frames) > max_keyframes:
            raise VideoLimitError("关键帧数量超过限制")
        for frame in self.frames:
            frame.keyframe.validate_against_duration(metadata.duration_ms)
        return list(self.frames)


@runtime_checkable
class ASRProvider(Protocol):
    """Transcribe audio bytes into timestamped segments without prescribing a vendor API."""

    async def transcribe(
        self,
        audio: bytes,
        *,
        language: str | None = None,
        duration_ms: int | None = None,
    ) -> Sequence[TranscriptSegment]: ...


@runtime_checkable
class VisionProvider(Protocol):
    """Describe a selected keyframe without prescribing a vendor API."""

    async def analyze(
        self,
        image: bytes,
        *,
        keyframe: Keyframe,
    ) -> Sequence[VisualEvent]: ...


@runtime_checkable
class OCRProvider(Protocol):
    """Optional, provider-neutral OCR contract for selected video frames."""

    async def extract(
        self,
        image: bytes,
        *,
        keyframe: Keyframe,
    ) -> str | None: ...


class FakeASRProvider:
    """A deterministic ASR fake suitable for unit tests and offline fixtures."""

    provider = "fake-asr"
    tool_version = "fake-asr-v1"
    model = "fake-asr-v1"

    def __init__(self, segments: Sequence[TranscriptSegment] | None = None) -> None:
        self._segments = None if segments is None else tuple(segments)
        self.calls = 0

    async def transcribe(
        self,
        audio: bytes,
        *,
        language: str | None = None,
        duration_ms: int | None = None,
    ) -> list[TranscriptSegment]:
        self.calls += 1
        if self._segments is not None:
            for segment in self._segments:
                segment.validate_against_duration(duration_ms)
            return list(self._segments)

        if duration_ms == 0:
            return []
        end_ms = duration_ms if duration_ms is not None else 1_000
        digest = sha256(audio).hexdigest()[:12]
        return [
            TranscriptSegment(
                start_ms=0,
                end_ms=end_ms,
                duration_ms=duration_ms,
                text=f"fake transcript {digest}",
                language=language,
                confidence=1.0,
            )
        ]


class FakeVisionProvider:
    """A deterministic Vision fake that never reads files or calls a network."""

    provider = "fake-vision"
    tool_version = "fake-vision-v1"
    model = "fake-vision-v1"

    def __init__(self, events: Sequence[VisualEvent] | None = None) -> None:
        self._events = None if events is None else tuple(events)
        self.calls = 0

    async def analyze(
        self,
        image: bytes,
        *,
        keyframe: Keyframe,
    ) -> list[VisualEvent]:
        self.calls += 1
        if self._events is not None:
            for event in self._events:
                event.validate_against_duration(keyframe.end_ms)
            return list(self._events)

        digest = sha256(image).hexdigest()[:12]
        return [
            VisualEvent(
                start_ms=keyframe.start_ms,
                end_ms=keyframe.end_ms,
                duration_ms=keyframe.duration_ms,
                event_type="other",
                summary=f"fake visual observation {digest}",
                keyframe_ids=[keyframe.keyframe_id],
            )
        ]


class FakeOCRProvider:
    """Deterministic OCR fake; it never reads files or calls a network."""

    provider = "fake-ocr"
    tool_version = "fake-ocr-v1"
    model = "fake-ocr-v1"

    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.calls = 0

    async def extract(self, image: bytes, *, keyframe: Keyframe) -> str | None:
        self.calls += 1
        return self.text


DeterministicASRProvider = FakeASRProvider
DeterministicVisionProvider = FakeVisionProvider


__all__ = [
    "ASRProvider",
    "AudioExtractionOptions",
    "AudioExtractor",
    "CommandResult",
    "CommandRunner",
    "DeterministicASRProvider",
    "DeterministicYtDlpNetworkExecutor",
    "DeterministicVisionProvider",
    "FakeAudioExtractor",
    "FakeASRProvider",
    "FakeSceneDetector",
    "FakeVideoSourceProvider",
    "FakeVisionProvider",
    "FakeOCRProvider",
    "FfmpegAudioExtractor",
    "FrameSample",
    "LoopbackYtDlpNetworkExecutor",
    "SceneDetector",
    "SubprocessCommandRunner",
    "VideoCapabilityError",
    "VideoDownloadOptions",
    "VideoDownloadResult",
    "VideoDownloader",
    "VideoLimitError",
    "VideoProviderError",
    "VideoSecurityError",
    "VideoSourceProvider",
    "YtDlpExecutionResult",
    "YtDlpNetworkExecutor",
    "YtDlpDownloader",
    "YtDlpVideoSourceProvider",
    "VisionProvider",
    "OCRProvider",
]
